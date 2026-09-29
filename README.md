# Bad Apple!! inside The Farmer Was Replaced

A project that brings the legendary **Bad Apple!!** animation into the programming automation game *The Farmer Was Replaced*.

## 📁 Project Structure & File Descriptions

* **`convert/` folder** — Contains the tools for video preprocessing and compression:
  * `bad_apple` — The original video file (`inp.mp4`) used as the source material.
  * `gen` — An AI-assisted Python converter script. It handles advanced video processing: downsamples the frame rate to match the game logic, resizes frames to a 100x100 grid, converts images to binary black-and-white, and compresses them using the **RLE (Run-Length Encoding)** algorithm for optimal game performance.

* **Core Visualization Scripts**:
  * `Bad_apple_grap` — The custom graphics engine **completely written by me from scratch**. It acts as the player/renderer that reads the compressed RLE data, decodes it on the fly, and controls the farm elements to draw the animation.
  * `Bad_apple_video` — The generated Python data module (`video_data.py`) containing the video metadata (dimensions, target FPS) and a massive array of RLE-encoded frames ready for playback.

## 🎬 How It Works

1. **RLE Compression**: The AI-assisted `gen` script reads the `bad_apple` video frame-by-frame. It downsizes each frame, binarizes it, flattens the matrix, and compresses continuous pixel blocks into pairs of numbers `[length, value]` to keep the file lightweight. The data is saved into `Bad_apple_video`.
2. **Real-time Rendering**: My custom-built player `Bad_apple_grap` reads these RLE lists, decompresses the pixel coordinates frame-by-frame according to the target FPS, and commands the in-game tiles to instantly change states, creating a fluid video playback directly on the farm field!
