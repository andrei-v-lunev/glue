import os
import shutil
import subprocess
import json
import logging
from flask import Flask, request, render_template, url_for, send_from_directory, jsonify
from werkzeug.utils import secure_filename
import threading
import time

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configure directories
UPLOAD_FOLDER = 'uploads'
EXPORT_FOLDER = os.path.join('static', 'exports')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EXPORT_FOLDER, exist_ok=True)

# Allowed file extensions
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'mov', 'avi'}
ALLOWED_AUDIO_EXTENSIONS = {'mp3', 'wav'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['EXPORT_FOLDER'] = EXPORT_FOLDER

# Global progress tracking
processing_status = {
    'current': 0,
    'total': 0,
    'current_file': '',
    'is_processing': False
}

def update_progress(current, total, current_file):
    processing_status['current'] = current
    processing_status['total'] = total
    processing_status['current_file'] = current_file
    processing_status['is_processing'] = True

def reset_progress():
    processing_status['current'] = 0
    processing_status['total'] = 0
    processing_status['current_file'] = ''
    processing_status['is_processing'] = False

def allowed_video_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_VIDEO_EXTENSIONS

def allowed_audio_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_AUDIO_EXTENSIONS

def clear_folder(folder_path):
    """Remove all files in the specified folder."""
    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        try:
            if os.path.isfile(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print(f"Error clearing folder {folder_path}: {e}")

def ffmpeg_concat(input_files, bg_music, output_path, bg_volume_factor, loop_bg_music=False):
    """
    Build and run an FFmpeg command that:
      - Preserves the original resolution (no scaling).
      - Forces sample aspect ratio=1 and uses yuv420p.
      - Resamples audio to 48000 Hz.
      - Concatenates the inputs with the concat filter.
      - If background music is provided, applies a volume filter using bg_volume_factor,
        resets timestamps with asetpts, then mixes it with amix.
      - Encodes using VideoToolbox with a fast preset and moves the moov atom to the start.
    """
    cmd = ["ffmpeg", "-y", "-progress", "pipe:1"]
    
    # Add video inputs
    for infile in input_files:
        cmd.extend(["-i", infile])
    if bg_music:
        cmd.extend(["-i", bg_music])
    
    n = len(input_files)
    filter_parts = []
    for i in range(n):
        filter_parts.append(f"[{i}:v]setsar=1,format=yuv420p[v{i}];")
        filter_parts.append(f"[{i}:a]aresample=48000[a{i}];")
    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
    filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[vid][aud];")
    
    # Calculate video duration
    video_duration_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1"]
    total_video_duration = 0
    for infile in input_files:
        duration = float(subprocess.check_output(video_duration_cmd + [infile]).decode().strip())
        total_video_duration += duration
    
    if bg_music:
        bg_music_duration = float(subprocess.check_output(video_duration_cmd + [bg_music]).decode().strip())
        
        if loop_bg_music and bg_music_duration < total_video_duration:
            # Loop background music for entire video duration
            loop_count = int(total_video_duration / bg_music_duration) + 1
            filter_parts.append(f"[{n}:a]aloop=loop={loop_count}:size=0,volume={bg_volume_factor},asetpts=PTS-STARTPTS[bg];")
            filter_parts.append(f"[aud][bg]amix=inputs=2:duration=longest[outa]")
        else:
            # Don't loop - just use background music for its duration and then continue with original audio
            filter_parts.append(f"[{n}:a]volume={bg_volume_factor},asetpts=PTS-STARTPTS[bg];")
            filter_parts.append(f"[aud][bg]amix=inputs=2:duration=first[outa]")
        
        final_audio = "[outa]"
    else:
        # Use original audio without modification
        final_audio = "[aud]"
    
    filter_complex = " ".join(filter_parts)
    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(["-map", "[vid]", "-map", final_audio])
    
    cmd.extend([
        "-c:v", "h264_videotoolbox",
        "-preset", "fast",
        "-c:a", "aac",
        "-movflags", "+faststart",
        output_path
    ])
    
    print("Running FFmpeg command:")
    print(" ".join(cmd))
    
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True
    )
    
    # Get total duration from first input file
    probe_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", input_files[0]]
    total_duration = float(subprocess.check_output(probe_cmd).decode().strip())
    
    # Read FFmpeg progress
    while True:
        line = process.stdout.readline()
        if not line:
            break
        if "out_time_ms=" in line:
            try:
                time_ms = int(line.split("=")[1])
                current_duration = float(time_ms) / 1000000  # Convert to seconds
                progress = min(100, int(current_duration * 100 / total_duration))
                update_progress(
                    progress,
                    processing_status['total'],
                    os.path.basename(output_path)
                )
            except:
                pass
    
    process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)

def process_videos(hook_items, lead_items, body_item, bg_music_path, bg_volume_db, loop_bg_music=False):
    """
    For each combination of hook, lead, and body (each provided as a tuple (abs_path, base_filename)),
    build the final output using direct FFmpeg commands.
    """
    download_links = []
    
    # Only check for some inputs (at least one of hook, lead, or body)
    if not (hook_items or lead_items or body_item):
        return download_links
    
    bg_volume_factor = 10 ** (bg_volume_db / 20.0)
    body_items = [body_item] if body_item else []
    
    def ensure_nonempty(lst):
        return lst if lst else [None]
    
    hook_items = ensure_nonempty(hook_items)
    lead_items = ensure_nonempty(lead_items)
    body_items = ensure_nonempty(body_items)
    
    clear_folder(EXPORT_FOLDER)
    
    # Calculate total number of combinations
    total_combinations = len(hook_items) * len(lead_items) * len(body_items)
    update_progress(0, total_combinations, "Starting processing...")
    
    current_combination = 0
    for i, hook in enumerate(hook_items):
        for j, lead in enumerate(lead_items):
            for k, body in enumerate(body_items):
                input_files = []
                hook_label = hook[1] if hook is not None else "hook-skip"
                lead_label = lead[1] if lead is not None else "lead-skip"
                body_label = body[1] if body is not None else "body-skip"
                output_filename = f"{hook_label}_{lead_label}_{body_label}.mp4"
                output_path = os.path.join(EXPORT_FOLDER, output_filename)
                
                if hook is not None:
                    input_files.append(hook[0])
                if lead is not None:
                    input_files.append(lead[0])
                if body is not None:
                    input_files.append(body[0])
                
                if not input_files:
                    continue
                
                try:
                    ffmpeg_concat(input_files, bg_music_path, output_path, bg_volume_factor, loop_bg_music)
                    current_combination += 1
                    update_progress(current_combination, total_combinations, output_filename)
                except subprocess.CalledProcessError as e:
                    print(f"Error during FFmpeg processing for {output_filename}: {e}")
                    continue
                
                download_links.append({
                    'filename': output_filename,
                    'url': url_for('download_file', filename=output_filename)
                })
    
    reset_progress()
    return download_links

@app.route('/progress')
def get_progress():
    return jsonify(processing_status)

@app.route('/', methods=['GET', 'POST'])
def index():
    download_links = []
    
    if request.method == 'POST':
        logger.info("POST request received")
        logger.info(f"Form data: {request.form}")
        logger.info(f"Files: {list(request.files.keys())}")
        
        # Check if any files were uploaded
        hooks_files = request.files.getlist("hooks")
        leads_files = request.files.getlist("leads")
        body_file = request.files.get("body")
        
        has_hooks = any(f and f.filename for f in hooks_files)
        has_leads = any(f and f.filename for f in leads_files)
        has_body = body_file and body_file.filename
        
        # Ensure at least one video was uploaded
        if not (has_hooks or has_leads or has_body):
            logger.warning("No videos were uploaded")
            return render_template('index.html', error="Please upload at least one video (hook, lead, or body)")
        
        reset_progress()
        # Clear old uploads (optional)
        clear_folder(UPLOAD_FOLDER)
        
        # Calculate total files to upload
        total_files = sum(1 for f in hooks_files if f and f.filename)
        total_files += sum(1 for f in leads_files if f and f.filename)
        if has_body:
            total_files += 1
        if request.files.get("bgmusic") and request.files.get("bgmusic").filename:
            total_files += 1
            
        logger.info(f"Total files to process: {total_files}")
        current_file = 0
        
        # Save hooks – store as tuple (absolute_path, base_filename)
        hook_items = []
        for f in hooks_files:
            if f and f.filename and allowed_video_file(f.filename):
                filename = secure_filename(f.filename)
                path = os.path.join(UPLOAD_FOLDER, filename)
                f.save(path)
                abs_path = os.path.abspath(path)
                base_name = os.path.splitext(filename)[0]
                hook_items.append((abs_path, base_name))
                current_file += 1
                update_progress(current_file, total_files, f"Uploading: {filename}")
        
        # Save leads – store as tuple (absolute_path, base_filename)
        lead_items = []
        for f in leads_files:
            if f and f.filename and allowed_video_file(f.filename):
                filename = secure_filename(f.filename)
                path = os.path.join(UPLOAD_FOLDER, filename)
                f.save(path)
                abs_path = os.path.abspath(path)
                base_name = os.path.splitext(filename)[0]
                lead_items.append((abs_path, base_name))
                current_file += 1
                update_progress(current_file, total_files, f"Uploading: {filename}")
        
        # Save body (optional) – store as tuple (absolute_path, base_filename)
        body_item = None
        if body_file and allowed_video_file(body_file.filename):
            filename = secure_filename(body_file.filename)
            path = os.path.join(UPLOAD_FOLDER, filename)
            body_file.save(path)
            abs_path = os.path.abspath(path)
            base_name = os.path.splitext(filename)[0]
            body_item = (abs_path, base_name)
            current_file += 1
            update_progress(current_file, total_files, f"Uploading: {filename}")
        
        # Save background music (optional)
        bg_music_path = None
        bg_music_file = request.files.get("bgmusic")
        if bg_music_file and allowed_audio_file(bg_music_file.filename):
            filename = secure_filename(bg_music_file.filename)
            path = os.path.join(UPLOAD_FOLDER, filename)
            bg_music_file.save(path)
            bg_music_path = os.path.abspath(path)
            current_file += 1
            update_progress(current_file, total_files, f"Uploading: {filename}")
        
        # Get background music volume
        bg_volume_db = float(request.form.get("bgvolume", -15))
        
        # Get background music loop setting
        loop_bg_music = 'loop_bg_music' in request.form
        
        # Process videos
        download_links = process_videos(hook_items, lead_items, body_item, bg_music_path, bg_volume_db, loop_bg_music)
    
    return render_template('index.html', download_links=download_links)

@app.route('/download/<path:filename>')
def download_file(filename):
    return send_from_directory(EXPORT_FOLDER, filename, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True, port=1000)
