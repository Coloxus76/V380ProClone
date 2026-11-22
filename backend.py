import os
import sys
import time
import socket
import threading
import subprocess
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Response, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
import cv2

app = FastAPI(title="V380 Pro Clone - Sin Publicidad", version="1.0.0")

CAMERA_IP = "192.168.1.20"
CAMERA_ID = "102710996"
CAMERA_MAC = "5c:4e:ee:b0:f9:c9"
RTSP_URL = f"rtsp://{CAMERA_IP}:554/live/ch0"
ONVIF_PTZ_URL = f"http://{CAMERA_IP}:8899/onvif/ptz_service"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOTS_DIR = os.path.join(BASE_DIR, "snapshots")
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
STATIC_DIR = os.path.join(BASE_DIR, "static")

os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

# ----------------- STREAM MANAGER -----------------
class StreamManager:
    def __init__(self, rtsp_url):
        self.rtsp_url = rtsp_url
        self.latest_frame = None
        self.latest_jpeg = None
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.fps = 0
        self.last_frame_time = 0
        self.connected = False

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()

    def _capture_loop(self):
        while self.running:
            cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                self.connected = False
                time.sleep(2.0)
                continue

            self.connected = True
            frame_count = 0
            start_time = time.time()

            while self.running and cap.isOpened():
                ret, frame = cap.read()
                if not ret or frame is None:
                    break

                frame_count += 1
                now = time.time()
                if now - start_time >= 1.0:
                    self.fps = round(frame_count / (now - start_time), 1)
                    frame_count = 0
                    start_time = now

                # Encode to JPEG
                success, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if success:
                    jpeg_bytes = encoded.tobytes()
                    with self.lock:
                        self.latest_frame = frame
                        self.latest_jpeg = jpeg_bytes
                        self.last_frame_time = now

            self.connected = False
            cap.release()
            time.sleep(1.0)

    def get_latest_jpeg(self):
        with self.lock:
            return self.latest_jpeg

    def get_latest_frame(self):
        with self.lock:
            return self.latest_frame.copy() if self.latest_frame is not None else None

stream_manager = StreamManager(RTSP_URL)
stream_manager.start()

# ----------------- RECORDING MANAGER -----------------
class RecordingManager:
    def __init__(self, rtsp_url, output_dir):
        self.rtsp_url = rtsp_url
        self.output_dir = output_dir
        self.process: Optional[subprocess.Popen] = None
        self.current_filename = None
        self.start_time = 0
        self.lock = threading.Lock()

    def start_recording(self):
        with self.lock:
            if self.is_recording():
                return False, "Ya se está grabando un video"

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.current_filename = f"grabacion_{ts}.mp4"
            output_path = os.path.join(self.output_dir, self.current_filename)

            cmd = [
                "ffmpeg",
                "-rtsp_transport", "tcp",
                "-i", self.rtsp_url,
                "-c:v", "copy",
                "-c:a", "aac",
                "-y",
                output_path
            ]

            # Start ffmpeg in background with stdin pipe so we can write 'q'
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self.start_time = time.time()
            return True, self.current_filename

    def stop_recording(self):
        with self.lock:
            if not self.is_recording():
                return False, "No hay grabación activa"

            try:
                if self.process and self.process.stdin:
                    self.process.stdin.write(b'q')
                    self.process.stdin.flush()
                self.process.wait(timeout=3.0)
            except Exception:
                if self.process:
                    self.process.terminate()

            saved_file = self.current_filename
            self.process = None
            self.current_filename = None
            self.start_time = 0
            return True, saved_file

    def is_recording(self):
        if self.process is not None and self.process.poll() is None:
            return True
        return False

    def get_status(self):
        with self.lock:
            recording = self.is_recording()
            duration = int(time.time() - self.start_time) if recording else 0
            return {
                "is_recording": recording,
                "current_file": self.current_filename,
                "duration_seconds": duration
            }

recording_manager = RecordingManager(RTSP_URL, RECORDINGS_DIR)

# ----------------- ONVIF PTZ CLIENT -----------------
def send_onvif_soap(body: str) -> bool:
    soap = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
  <soap:Body>
    {body}
  </soap:Body>
</soap:Envelope>"""
    req = f"POST /onvif/ptz_service HTTP/1.1\r\nHost: {CAMERA_IP}:8899\r\nContent-Type: application/soap+xml; charset=utf-8\r\nContent-Length: {len(soap)}\r\nConnection: close\r\n\r\n{soap}"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.5)
        s.connect((CAMERA_IP, 8899))
        s.sendall(req.encode('ascii'))
        resp = s.recv(2048).decode('ascii', errors='ignore')
        s.close()
        return "200 OK" in resp
    except Exception as e:
        print(f"[PTZ] Error: {e}")
        return False

class PTZMoveRequest(BaseModel):
    direction: str  # up, down, left, right
    speed: float = 0.4
    duration: float = 0.35

@app.post("/api/ptz/move")
def ptz_move(req: PTZMoveRequest):
    vx, vy = 0.0, 0.0
    spd = max(0.1, min(1.0, req.speed))
    d = req.direction.lower()

    if d == "up":
        vy = spd
    elif d == "down":
        vy = -spd
    elif d == "left":
        vx = -spd
    elif d == "right":
        vx = spd
    elif d == "up-left":
        vx, vy = -spd, spd
    elif d == "up-right":
        vx, vy = spd, spd
    elif d == "down-left":
        vx, vy = -spd, -spd
    elif d == "down-right":
        vx, vy = spd, -spd
    else:
        raise HTTPException(status_code=400, detail="Dirección inválida")

    move_xml = f"""<tptz:ContinuousMove>
      <tptz:ProfileToken>PROFILE_000</tptz:ProfileToken>
      <tptz:Velocity>
        <tt:PanTilt x="{vx}" y="{vy}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"/>
      </tptz:Velocity>
    </tptz:ContinuousMove>"""
    ok = send_onvif_soap(move_xml)

    # Optional auto-stop after duration
    if req.duration > 0:
        def stop_after_delay():
            time.sleep(req.duration)
            ptz_stop()
        threading.Thread(target=stop_after_delay, daemon=True).start()

    return {"status": "ok" if ok else "error", "direction": d}

@app.post("/api/ptz/stop")
def ptz_stop():
    stop_xml = """<tptz:Stop>
      <tptz:ProfileToken>PROFILE_000</tptz:ProfileToken>
      <tptz:PanTilt>true</tptz:PanTilt>
      <tptz:Zoom>true</tptz:Zoom>
    </tptz:Stop>"""
    ok = send_onvif_soap(stop_xml)
    return {"status": "ok" if ok else "error"}

# ----------------- STREAM ENDPOINTS -----------------
def generate_mjpeg_frames():
    while True:
        frame_bytes = stream_manager.get_latest_jpeg()
        if frame_bytes:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.04)  # ~25 FPS loop

@app.get("/api/stream")
def get_stream():
    return StreamingResponse(
        generate_mjpeg_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.get("/api/snapshot")
def get_snapshot():
    frame_bytes = stream_manager.get_latest_jpeg()
    if not frame_bytes:
        raise HTTPException(status_code=503, detail="Cámara desconectada")
    return Response(content=frame_bytes, media_type="image/jpeg")

@app.post("/api/snapshot/save")
def save_snapshot():
    frame = stream_manager.get_latest_frame()
    if frame is None:
        raise HTTPException(status_code=503, detail="Cámara desconectada")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"foto_{ts}.jpg"
    filepath = os.path.join(SNAPSHOTS_DIR, filename)
    cv2.imwrite(filepath, frame)
    return {
        "status": "ok",
        "filename": filename,
        "url": f"/api/snapshots/{filename}",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/snapshots")
def list_snapshots():
    files = []
    if os.path.exists(SNAPSHOTS_DIR):
        for f in os.listdir(SNAPSHOTS_DIR):
            if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                p = os.path.join(SNAPSHOTS_DIR, f)
                stat = os.stat(p)
                files.append({
                    "filename": f,
                    "url": f"/api/snapshots/{f}",
                    "size_kb": round(stat.st_size / 1024, 1),
                    "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                })
    files.sort(key=lambda x: x["created_at"], reverse=True)
    return files

@app.get("/api/snapshots/{filename}")
def serve_snapshot(filename: str):
    p = os.path.join(SNAPSHOTS_DIR, os.path.basename(filename))
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(p, media_type="image/jpeg")

@app.delete("/api/snapshots/{filename}")
def delete_snapshot(filename: str):
    p = os.path.join(SNAPSHOTS_DIR, os.path.basename(filename))
    if os.path.exists(p):
        os.remove(p)
        return {"status": "ok", "deleted": filename}
    raise HTTPException(status_code=404, detail="Archivo no encontrado")

# ----------------- RECORDING ENDPOINTS -----------------
@app.post("/api/record/start")
def start_record():
    ok, result = recording_manager.start_recording()
    if not ok:
        raise HTTPException(status_code=400, detail=result)
    return {"status": "ok", "filename": result}

@app.post("/api/record/stop")
def stop_record():
    ok, result = recording_manager.stop_recording()
    if not ok:
        raise HTTPException(status_code=400, detail=result)
    return {"status": "ok", "saved_file": result}

@app.get("/api/record/status")
def get_record_status():
    return recording_manager.get_status()

@app.get("/api/recordings")
def list_recordings():
    files = []
    if os.path.exists(RECORDINGS_DIR):
        for f in os.listdir(RECORDINGS_DIR):
            if f.lower().endswith(('.mp4', '.mkv')):
                p = os.path.join(RECORDINGS_DIR, f)
                stat = os.stat(p)
                files.append({
                    "filename": f,
                    "url": f"/api/recordings/{f}",
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                })
    files.sort(key=lambda x: x["created_at"], reverse=True)
    return files

@app.get("/api/recordings/{filename}")
def serve_recording(filename: str):
    p = os.path.join(RECORDINGS_DIR, os.path.basename(filename))
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(p, media_type="video/mp4")

@app.delete("/api/recordings/{filename}")
def delete_recording(filename: str):
    p = os.path.join(RECORDINGS_DIR, os.path.basename(filename))
    if os.path.exists(p):
        os.remove(p)
        return {"status": "ok", "deleted": filename}
    raise HTTPException(status_code=404, detail="Archivo no encontrado")

# ----------------- SYSTEM STATUS -----------------
@app.get("/api/status")
def get_system_status():
    return {
        "camera_ip": CAMERA_IP,
        "camera_id": CAMERA_ID,
        "camera_mac": CAMERA_MAC,
        "rtsp_url": RTSP_URL,
        "connected": stream_manager.connected,
        "fps": stream_manager.fps,
        "recording": recording_manager.get_status(),
        "has_frame": stream_manager.latest_jpeg is not None,
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

# Serve static UI
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    print(f"==================================================")
    print(f"  V380 Pro Clone - Servidor Iniciado")
    print(f"  URL Local: http://localhost:8080")
    print(f"  Cámara IP: {CAMERA_IP} (RTSP 554, ONVIF 8899)")
    print(f"==================================================")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
