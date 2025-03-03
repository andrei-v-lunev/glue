import os
import shutil
import subprocess
from flask import Flask, request, render_template, url_for, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Configure directories
UPLOAD_FOLDER = 'uploads'
EXPORT_FOLDER = os.path.join('static', 'exports')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EXPORT_FOLDER, exist_ok=True)

# Allowed file extensions
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'mov', 'avi'}
ALLOWED_AUDIO_EXTENSIONS = {'mp3'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['EXPORT_FOLDER'] = EXPORT_FOLDER

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

def ffmpeg_concat(input_files, bg_music, output_path, bg_volume_factor):
    """
    Build and run an FFmpeg command that:
      - Preserves the original resolution (no scaling).
      - Forces sample aspect ratio=1 and uses yuv420p.
      - Resamples audio to 48000 Hz.
      - Concatenates the inputs with the concat filter.
      - If background music is provided, applies a volume filter using bg_volume_factor,
        resets timestamps with asetpts, then mixes it with amix.
      - Encodes using VideoToolbox with a fast preset and moves the moov atom to the start.
    
    Parameters:
      input_files: list of video file paths.
      bg_music: path to background music file (or None).
      output_path: full output file path.
      bg_volume_factor: linear factor for background music volume.
    """
    cmd = ["ffmpeg", "-y"]
    
    # Add video inputs
    for infile in input_files:
        cmd.extend(["-i", infile])
    # If background music is provided, add as extra input.
    if bg_music:
        cmd.extend(["-i", bg_music])
    
    n = len(input_files)
    filter_parts = []
    # For each video input: preserve original resolution; set SAR=1; force pixel format; and resample audio.
    for i in range(n):
        filter_parts.append(f"[{i}:v]setsar=1,format=yuv420p[v{i}];")
        filter_parts.append(f"[{i}:a]aresample=48000[a{i}];")
    # Concatenate the streams.
    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
    filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[vid][aud];")
    
    if bg_music:
        # Process background music: apply volume and reset timestamps.
        filter_parts.append(f"[{n}:a]volume={bg_volume_factor},asetpts=PTS-STARTPTS[bg];")
        filter_parts.append(f"[aud][bg]amix=inputs=2:duration=shortest[outa]")
        final_audio = "[outa]"
    else:
        final_audio = "[aud]"
    
    filter_complex = " ".join(filter_parts)
    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(["-map", "[vid]", "-map", final_audio])
    
    # Use VideoToolbox, fast preset, and faststart flag.
    cmd.extend([
        "-c:v", "h264_videotoolbox",
        "-preset", "fast",
        "-c:a", "aac",
        "-movflags", "+faststart",
        output_path
    ])
    
    print("Running FFmpeg command:")
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)

def process_videos(hook_items, lead_items, body_item, bg_music_path, bg_volume_db):
    """
    For each combination of hook, lead, and body (each provided as a tuple (abs_path, base_filename)),
    build the final output using direct FFmpeg commands.
    
    The output filename is built from the base names (or "segment-skip" if missing).
    Returns a list of download link dictionaries.
    """
    download_links = []
    
    if not hook_items and not lead_items and not body_item:
        return download_links
    
    # Compute background volume factor (e.g., -15 dB -> 10^(-15/20))
    bg_volume_factor = 10 ** (bg_volume_db / 20.0)
    
    # Treat body as a list for uniform iteration.
    body_items = [body_item] if body_item else []
    
    def ensure_nonempty(lst):
        return lst if lst else [None]
    
    hook_items = ensure_nonempty(hook_items)
    lead_items = ensure_nonempty(lead_items)
    body_items = ensure_nonempty(body_items)
    
    clear_folder(EXPORT_FOLDER)
    
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
                    ffmpeg_concat(input_files, bg_music_path, output_path, bg_volume_factor)
                except subprocess.CalledProcessError as e:
                    print(f"Error during FFmpeg processing for {output_filename}: {e}")
                    continue
                
                download_links.append({
                    'filename': output_filename,
                    'url': url_for('download_file', filename=output_filename)
                })
    
    return download_links

@app.route('/', methods=['GET', 'POST'])
def index():
    download_links = []
    
    if request.method == 'POST':
        # Clear old uploads (optional)
        clear_folder(UPLOAD_FOLDER)
        
        # Save hooks – store as tuple (absolute_path, base_filename)
        hooks_files = request.files.getlist("hooks")
        hook_items = []
        for f in hooks_files:
            if f and allowed_video_file(f.filename):
                filename = secure_filename(f.filename)
                path = os.path.join(UPLOAD_FOLDER, filename)
                f.save(path)
                abs_path = os.path.abspath(path)
                base_name = os.path.splitext(filename)[0]
                hook_items.append((abs_path, base_name))
        
        # Save leads – store as tuple (absolute_path, base_filename)
        leads_files = request.files.getlist("leads")
        lead_items = []
        for f in leads_files:
            if f and allowed_video_file(f.filename):
                filename = secure_filename(f.filename)
                path = os.path.join(UPLOAD_FOLDER, filename)
                f.save(path)
                abs_path = os.path.abspath(path)
                base_name = os.path.splitext(filename)[0]
                lead_items.append((abs_path, base_name))
        
        # Save body (optional) – store as tuple (absolute_path, base_filename)
        body_file = request.files.get("body")
        body_item = None
        if body_file and allowed_video_file(body_file.filename):
            filename = secure_filename(body_file.filename)
            path = os.path.join(UPLOAD_FOLDER, filename)
            body_file.save(path)
            abs_path = os.path.abspath(path)
            base_name = os.path.splitext(filename)[0]
            body_item = (abs_path, base_name)
        
        # Save background music (optional)
        bg_music_file = request.files.get("bgmusic")
        bg_music_path = None
        if bg_music_file and allowed_audio_file(bg_music_file.filename):
            filename = secure_filename(bg_music_file.filename)
            bg_music_path = os.path.join(UPLOAD_FOLDER, filename)
            bg_music_file.save(bg_music_path)
            bg_music_path = os.path.abspath(bg_music_path)
        
        # Get background music volume in dB (e.g., -15)
        bg_volume_db = float(request.form.get("bgvolume", -15))
        
        download_links = process_videos(
            hook_items=hook_items,
            lead_items=lead_items,
            body_item=body_item,
            bg_music_path=bg_music_path,
            bg_volume_db=bg_volume_db
        )
        
        # After processing is done, clear the uploads folder to free up space.
        clear_folder(UPLOAD_FOLDER)
    
    return render_template('index.html', download_links=download_links)

@app.route('/download/<path:filename>')
def download_file(filename):
    return send_from_directory(EXPORT_FOLDER, filename, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)
