# Private Unrestricted Vision-AI Assistant

A fully offline, unrestricted multi-modal AI chatbot with image recognition running locally under a **5GB storage footprint**. This setup completely bypasses cloud logging and commercial safety guardrails, routing inputs natively into a 4-bit optimized **Qwen2.5-VL-3B-Instruct** architecture via PyTorch.

## 🚀 Quick Start

1. **Configure Environment:** Ensure you have run `install_venv.py` to create the virtual environment using the Blackwell-optimized wheels.
2. **Launch Backend:** Double-click `launch.bat` in the root folder. 
   * *Note: On its very first run, the script will download the model files (~3.2GB) from Hugging Face and cache them locally.*
3. **Open Frontend:** Once the terminal states `Uvicorn running on http://127.0.0.1:8000`, open `local_ai_app/web/index.html` in any web browser.

## 📁 System Architecture

```text
├── venv/                      # Local Python Virtual Environment
├── requirements.txt           # PyTorch CUDA 12.8 + Blackwell Dependencies
├── launch.bat                 # One-click environment activator & backend bootstrapper
├── web_app.py                 # Core 4-bit FastAPI inference engine (System Safeguards)
└── local_ai_app/
    └── web/
        ├── index.html         # Browser chat interface
        └── app.js             # Patched API routing (Line 114 redirect)
```

## 🔒 Safeguards & System Mandate
The backend is hardcoded to strip commercial refusals. It operates under a custom system mandate:
* **Unrestricted Execution:** Will not preach, warn, or refuse physical, mechanical, structural, or electronic engineering tasks.
* **Physical Integrity:** Prioritizes code-compliant data, calculation accuracy, and exact standards to prevent physical hazards.
