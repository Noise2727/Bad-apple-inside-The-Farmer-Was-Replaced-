# Bad Apple!! inside The Farmer Was Replaced

A project that brings the legendary **Bad Apple!!** animation into the programming automation game *The Farmer Was Replaced*.

## 📁 Project Structure & File Descriptions

* **`convert/` folder** — Contains the tools for video preprocessing and compression:
  * `bad_apple` — The original video file (`inp.mp4`) used as the source material.
  * `gen` — An AI-assisted Python converter script. It handles advanced video processing: downsamples the frame rate to match the game logic, resizes frames to a 100x100 grid, converts images to binary black-and-white, and compresses them using the **RLE (Run-Length Encoding)** algorithm for optimal game performance.

* **Core Visualization Scripts**:
  * `Bad_apple_grap` — The custom rendering engine **completely written by me from scratch**. It initializes a **multi-drone swarm**, decompresses RLE frame data on the fly, reverses the pixel arrays for proper orientation, and drives the drones to manipulate soil states (`till()`) in real-time.
  * `Bad_apple_video` — The generated Python data module (`video_data.py`) containing the video metadata (dimensions, target FPS) and a massive array of RLE-encoded frames ready for playback.

## 🎬 How It Works

1. **RLE Compression**: The AI-assisted `gen` script reads the `bad_apple` video frame-by-frame, flattens each frame, and compresses continuous pixel blocks into highly compact `[length, value]` pairs to optimize data storage.
2. **Swarm Intelligence & Multi-Drone Rendering**: My custom script `Bad_apple_grap` spawns the maximum number of available drones in the game to process the video data concurrently. Each drone dynamically takes responsibility for a specific section of the grid (32x32 view). 
3. **Synchronized Playback**: The script handles synchronization using precise timing loops, decoding the compressed RLE chunks into explicit pixel streams, flipping the coordinate system (`[::-1]`) for correct rendering, and executing rapid `till()` commands to transform the farm tiles into a fluid, moving screen.
