# Licensed under the General Public License 3.0

import bpy
import threading
import queue
import requests
import re

response_queue = None

def init():
    global response_queue
    response_queue = queue.Queue()

def worker_check(key, result_queue):
    data = {
        'api_dev_key': key,
        'api_option': 'paste',
        'api_paste_code': "API Validation Test",
        'api_paste_expire_date': '10M'
    }
    try:
        response = requests.post("https://pastebin.com/api/api_post.php", data=data)
        if "Bad API request" in response.text:
            print(response.text, "api key:", key)
            result_queue.put((False, response.text))
        else:
            result_queue.put((True, response.text))
    except requests.RequestException as e:
        result_queue.put((False, str(e)))

def check_queue():
    if response_queue is not None:
        try:
            success, message = response_queue.get_nowait()

            if success:
                print("Pastebin key is valid!")
                bpy.types.Scene.api_key_valid = True
            else:
                print(f"Pastebin validation failed: {message}")
                bpy.types.Scene.api_key_valid = False

            return None
        
        except queue.Empty:
            return 0.1

    return None

def check_pastebin(self, context):
    api_key = context.scene.pastebinAPI.strip()

    thread = threading.Thread(
        target=worker_check,
        args=(api_key, response_queue),
        daemon=True
    )
    thread.start()
    bpy.app.timers.register(check_queue)

    print("Validating Pastebin API key...")

def post_pastebin(payload, context):
    data = {
        'api_dev_key': context.scene.pastebinAPI.strip(),
        'api_option': 'paste',
        'api_paste_code': payload,
        'api_paste_expire_date': 'N'
    }

    try:
        response = requests.post("https://pastebin.com/api/api_post.php", data=data)

        print(response, response.text)

        if "Bad API request" in response.text:
            print(response.text)
            return {"CANCELLED"}
        
        context.window_manager.clipboard = response.text
        
    except Exception as e:
        print(f"Failed to upload to pastebin: {e}")
        return {"CANCELLED"}

def get_pastebin(url, context):
    clipboard_text = context.window_manager.clipboard.strip()

    match = re.search(r"pastebin\.com/(?:raw/)?([a-zA-Z0-9]+)", clipboard_text)
    if not match:
        print("Clipboard does not contain a valid Pastebin URL")
        return {"CANCELLED"}

    paste_id = match.group(1)
    raw_url = f"https://pastebin.com/raw/{paste_id}"

    try:
        response = requests.get(raw_url, timeout=5)
        response.raise_for_status()
        payload = response.text

    except Exception as e:
        print("Error occured while reading paste:", e)

    return payload