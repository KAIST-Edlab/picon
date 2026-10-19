import asyncio
import argparse
from aiohttp import web, ClientSession, ClientTimeout, WSMsgType, WSServerHandshakeError



# Arguments
parser = argparse.ArgumentParser()
parser.add_argument('--proxy_host', default='127.0.0.1', type=str, required=False, help='Proxy listening host address')
parser.add_argument('--proxy_port', default=9978, type=int, required=False, help='Proxy listening port (ngrok target)')
parser.add_argument('--backend_url', type=str, required=True, help='Real backend URL (e.g. http://1.2.3.4:5579)')
args = parser.parse_args()

BACKEND_URL = args.backend_url.rstrip('/')

# Hop-by-hop headers must not be forwarded
HOP_BY_HOP = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade', 'host', 'content-length',
}



def build_headers(request: web.Request) -> dict:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}

    # Keep the original client IP at the front so the backend rate limit works per user
    peer = request.remote or ''
    forwarded_for = request.headers.get('X-Forwarded-For')
    headers['X-Forwarded-For'] = f'{forwarded_for}, {peer}' if forwarded_for else peer
    headers['X-Forwarded-Host'] = request.host
    headers['X-Forwarded-Proto'] = request.headers.get('X-Forwarded-Proto', request.scheme)
    return headers



async def proxy_websocket(request: web.Request) -> web.WebSocketResponse:
    session: ClientSession = request.app['session']
    target = BACKEND_URL.replace('http', 'ws', 1) + request.path_qs
    headers = {k: v for k, v in build_headers(request).items() if not k.lower().startswith('sec-websocket')}

    try:
        backend_ws = await session.ws_connect(target, headers=headers, autoping=True, heartbeat=30)
    except WSServerHandshakeError as e:
        # Backend rejected the handshake (e.g. unknown session_id), pass its status through
        return web.Response(status=e.status, text=e.message)
    except Exception as e:
        raise web.HTTPBadGateway(text=f'Backend WebSocket connection failed: {e}')

    client_ws = web.WebSocketResponse(autoping=True, heartbeat=30)
    await client_ws.prepare(request)

    async def pipe(src, dst):
        async for msg in src:
            if msg.type == WSMsgType.TEXT:
                await dst.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await dst.send_bytes(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break

    tasks = [
        asyncio.create_task(pipe(client_ws, backend_ws)),
        asyncio.create_task(pipe(backend_ws, client_ws)),
    ]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()

    # Propagate the backend close code (e.g. 4001 for unknown session) to the client
    close_code = backend_ws.close_code or 1000
    await backend_ws.close()
    await client_ws.close(code=close_code)
    return client_ws



async def proxy_handler(request: web.Request) -> web.StreamResponse:
    if request.headers.get('Upgrade', '').lower() == 'websocket':
        return await proxy_websocket(request)

    session: ClientSession = request.app['session']
    target = BACKEND_URL + request.path_qs
    body = await request.read()

    try:
        async with session.request(
            request.method, target, headers=build_headers(request), data=body or None, allow_redirects=False
        ) as backend_res:
            headers = {k: v for k, v in backend_res.headers.items() if k.lower() not in HOP_BY_HOP}
            response = web.StreamResponse(status=backend_res.status, headers=headers)
            await response.prepare(request)
            async for chunk in backend_res.content.iter_chunked(8192):
                await response.write(chunk)
            await response.write_eof()
            return response
    except web.HTTPException:
        raise
    except Exception as e:
        raise web.HTTPBadGateway(text=f'Backend request failed: {e}')



async def on_startup(app: web.Application):
    # No total timeout: simulations over WebSocket can run for a long time
    app['session'] = ClientSession(timeout=ClientTimeout(total=None, connect=10), auto_decompress=False)



async def on_cleanup(app: web.Application):
    await app['session'].close()



if __name__ == '__main__':
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route('*', '/{path:.*}', proxy_handler)
    print(f'Proxy {args.proxy_host}:{args.proxy_port} -> {BACKEND_URL}')
    web.run_app(app, host=args.proxy_host, port=args.proxy_port)
