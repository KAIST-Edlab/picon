"""
PICon Demo — HuggingFace Space entry point.

Serves the static demo page (index.html + assets) via FastAPI on port 7860.
All API calls from the browser go directly to the Railway backend.
"""

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI()


@app.get("/")
async def root():
    return FileResponse("index.html")


app.mount("/assets", StaticFiles(directory="assets"), name="assets")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
