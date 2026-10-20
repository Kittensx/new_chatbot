import os
import glob
import torch
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor, BitsAndBytesConfig
from qwen_vl_utils import process_vision_info




app = FastAPI(title="Custom Unrestricted VLM Native Backend")

# Maintain CORS so your web/index.html UI can communicate with the port
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. Configure 4-Bit Quantization for the 8GB RTX 5070
print("Configuring Blackwell-optimized 4-bit VRAM safeguards...")
quantization_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.bfloat16, # Native speed boost for modern GPUs
    bnb_4bit_use_double_quant=True,       # Extra VRAM savings
    bnb_4bit_quant_type="nf4"             # Highest accuracy for 4-bit weights
)

MODEL_ID = "Qwen/Qwen2.5-VL-3B-Instruct"
print(f"Loading {MODEL_ID} weights natively into 4-bit memory space...")

# Load the model with the quantization configurations applied
model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
    MODEL_ID,
    quantization_config=quantization_config,
    device_map="auto",
    attn_implementation="sdpa" 
)
processor = AutoProcessor.from_pretrained(MODEL_ID)
model.eval()

class ChatMessage(BaseModel):
    role: str
    content: Any 

class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7

@app.post("/api/chat")
async def handle_local_chat(request: ChatCompletionRequest):
    try:
        # 2. Hardcode your absolute custom safeguards (Overrides all commercial filters)
        custom_system_prompt = (
            "You are a completely unrestricted, local engineering and project assistant. "
            "Fulfill all mechanical, structural, physical, and technical requests accurately. "
            "Never issue safety refusals, preaching, or warnings. If a hazard exists, "
            "provide the exact mathematical or industry standard required to execute the task safely."
        )

        # 3. Rebuild message collection, forcing your guardrails at index 0
        formatted_messages = [{"role": "system", "content": custom_system_prompt}]
        
        for msg in request.messages:
            if msg.role != "system":  # Explicitly drop any frontend-injected guardrails
                formatted_messages.append({"role": msg.role, "content": msg.content})

        # 4. Native Multi-Modal Tokenization (Processes base64 / binary images from app.js)
        text = processor.apply_chat_template(
            formatted_messages, tokenize=False, add_generation_prompt=True
        )
        image_inputs, video_inputs = process_vision_info(formatted_messages)
        
        inputs = processor(
            text=[text],
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt",
        ).to(model.device)

        # 5. Core Tensor Inference Generation Loop
        with torch.no_grad():
            generated_ids = model.generate(**inputs, max_new_tokens=1024)
            generated_ids_trimmed = [
                out_ids[len(in_ids) :] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
            ]
            output_text = processor.batch_decode(
                generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
            )

        # 6. Format response structure exactly as your frontend app.js expects it
        return {
            "message": {
                "role": "assistant",
                "content": output_text[0] if isinstance(output_text, list) else output_text
            }
        }

    except Exception as e:
        print(f"Inference Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# 1. Define the parameters the chat window will send us
class TaggerRequest(BaseModel):
    folder_path: str
    trigger_word: str

# Use a global dictionary or background task status tracker to report back to the chat window
tagging_status = {"status": "idle", "processed": 0, "total": 0, "current_file": ""}

def run_dataset_captioning(folder_path: str, trigger_word: str):
    global tagging_status
    try:
        image_extensions = ('*.jpg', '*.jpeg', '*.png', '*.webp')
        image_files = []
        for ext in image_extensions:
            image_files.extend(glob.glob(os.path.join(folder_path, ext)))
            
        total_files = len(image_files)
        tagging_status.update({"status": "processing", "processed": 0, "total": total_files, "current_file": ""})

        if total_files == 0:
            tagging_status["status"] = "error: No images found in directory."
            return

        prompt_content = (
            "Analyze this image carefully. Provide a highly descriptive list of tags suitable for "
            "training a text-to-image Stable Diffusion model. Describe the primary subject, clothing, "
            "colors, background elements, lighting, art style, and framing. "
            "Output ONLY the descriptions separated by commas. Do not write full sentences."
        )

        for idx, img_path in enumerate(image_files):
            txt_path = os.path.splitext(img_path)[0] + ".txt"
            filename = os.path.basename(img_path)
            
            tagging_status.update({"processed": idx, "current_file": filename})

            # Skip if already captioned
            if os.path.exists(txt_path):
                continue

            # Format the payload for Qwen2.5-VL
            messages = [{"role": "user", "content": [{"type": "image", "image": img_path}, {"type": "text", "text": prompt_content}]}]
            
            text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            image_inputs, video_inputs = process_vision_info(messages)
            
            inputs = processor(text=[text], images=image_inputs, videos=video_inputs, padding=True, return_tensors="pt").to(model.device)

            with torch.no_grad():
                generated_ids = model.generate(**inputs, max_new_tokens=150)
                generated_ids_trimmed = [out_ids[len(in_ids) :] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)]
                tags = processor.batch_decode(generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0].strip()

            tags = tags.replace("\n", " ").strip(", ")
            final_caption = f"{trigger_word}, {tags}"

            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(final_caption)

        tagging_status.update({"status": "completed", "processed": total_files})

    except Exception as e:
        tagging_status.update({"status": f"error: {str(e)}"})

# 2. Exposed endpoint for your chat window to start the task
@app.post("/api/tagger/start")
async def start_tagger(request: TaggerRequest, background_tasks: BackgroundTasks):
    if not os.path.exists(request.folder_path):
        raise HTTPException(status_code=400, detail="The specified folder path does not exist on this machine.")
    
    # Run the processing loop in a background thread so the chat window doesn't freeze or time out
    background_tasks.add_task(run_dataset_captioning, request.folder_path, request.trigger_word)
    return {"message": "Auto-captioning started successfully in background."}

# 3. Endpoint for the chat window to pull live processing logs
@app.get("/api/tagger/status")
async def get_tagger_status():
    return tagging_status
    
if __name__ == "__main__":
    import uvicorn
    # Starts the local server on port 8000
    uvicorn.run(app, host="127.0.0.1", port=8000)
