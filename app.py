#!/usr/bin/env python3
"""
YouTube Video Downloader Backend
A Flask-based API for downloading YouTube videos using yt-dlp
"""

import os
import json
import uuid
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS
import yt_dlp
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)  # Enable CORS for frontend integration

# Configuration
DOWNLOAD_DIR = Path("downloads")
TEMP_DIR = Path("temp")
MAX_FILE_AGE_HOURS = 24  # Auto-cleanup after 24 hours
MAX_CONCURRENT_DOWNLOADS = 3

# Ensure directories exist
DOWNLOAD_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

# Global state for tracking downloads
active_downloads = {}
download_lock = threading.Lock()

class DownloadProgress:
    """Track download progress for real-time updates"""
    def __init__(self, download_id):
        self.download_id = download_id
        self.status = "starting"
        self.progress = 0
        self.filename = None
        self.error = None
        self.completed_at = None
        self.file_path = None

def progress_hook(d, download_id):
    """Progress hook for yt-dlp"""
    if download_id not in active_downloads:
        return
    
    progress_obj = active_downloads[download_id]
    
    if d['status'] == 'downloading':
        if 'total_bytes' in d and d['total_bytes']:
            progress_obj.progress = (d['downloaded_bytes'] / d['total_bytes']) * 100
        elif '_percent_str' in d:
            # Fallback to percentage string parsing
            percent_str = d['_percent_str'].strip().replace('%', '')
            try:
                progress_obj.progress = float(percent_str)
            except ValueError:
                pass
        progress_obj.status = "downloading"
        
    elif d['status'] == 'finished':
        progress_obj.status = "processing"
        progress_obj.progress = 90
        progress_obj.filename = Path(d['filename']).name
        
    elif d['status'] == 'error':
        progress_obj.status = "error"
        progress_obj.error = str(d.get('error', 'Unknown error'))

def get_video_info(url):
    """Extract video information without downloading"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Get available formats
            formats = []
            if 'formats' in info:
                for f in info['formats']:
                    if f.get('vcodec') != 'none' or f.get('acodec') != 'none':
                        formats.append({
                            'format_id': f.get('format_id'),
                            'ext': f.get('ext'),
                            'quality': f.get('format_note', f.get('quality', 'unknown')),
                            'filesize': f.get('filesize'),
                            'vcodec': f.get('vcodec'),
                            'acodec': f.get('acodec'),
                            'width': f.get('width'),
                            'height': f.get('height'),
                        })
            
            return {
                'title': info.get('title', 'Unknown Title'),
                'duration': info.get('duration'),
                'view_count': info.get('view_count'),
                'uploader': info.get('uploader'),
                'upload_date': info.get('upload_date'),
                'description': info.get('description', '')[:200] + '...' if info.get('description') else '',
                'thumbnail': info.get('thumbnail'),
                'formats': formats[:20],  # Limit to avoid too much data
                'webpage_url': info.get('webpage_url', url)
            }
    except Exception as e:
        raise Exception(f"Failed to extract video info: {str(e)}")

def download_video(url, quality, download_id, custom_format=None):
    """Download video in a separate thread"""
    try:
        progress_obj = active_downloads[download_id]
        
        # Determine output template and format
        output_template = str(TEMP_DIR / f"{download_id}_%(title)s.%(ext)s")
        
        # Enhanced yt-dlp options to bypass restrictions
        ydl_opts = {
            'outtmpl': output_template,
            'progress_hooks': [lambda d: progress_hook(d, download_id)],
            'quiet': False,  # Enable output for debugging
            'no_warnings': False,
            # Anti-detection measures
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'referer': 'https://www.youtube.com/',
            'extractor_retries': 3,
            'fragment_retries': 3,
            'retries': 3,
            'continuedl': True,
            'noplaylist': True,
            # Cookie and session handling
            'cookiefile': None,
            'nocheckcertificate': True,
            # Rate limiting to avoid detection
            'sleep_interval': 1,
            'max_sleep_interval': 5,
            'sleep_interval_requests': 1,
            # Use oauth2 if available
            'username': None,
            'password': None,
        }
        
        # Use custom format if provided, otherwise use quality-based selection
        if custom_format:
            ydl_opts['format'] = custom_format
            print(f"Using custom format: {custom_format}")
        else:
            # Set format based on quality selection - prioritize high quality
            if quality == 'best':
                # Try to get the absolute best quality available
                ydl_opts['format'] = (
                    'bestvideo[height>=2160]+bestaudio[ext=m4a]/bestvideo[height>=2160]+bestaudio/'  # 4K+
                    'bestvideo[height>=1440]+bestaudio[ext=m4a]/bestvideo[height>=1440]+bestaudio/'  # 1440p+
                    'bestvideo[height>=1080]+bestaudio[ext=m4a]/bestvideo[height>=1080]+bestaudio/'  # 1080p+
                    'bestvideo[height>=720]+bestaudio[ext=m4a]/bestvideo[height>=720]+bestaudio/'    # 720p+
                    'best[height>=1080]/best[height>=720]/best'  # Fallback to single file formats
                )
            elif quality == 'audio':
                ydl_opts['format'] = (
                    'bestaudio[ext=m4a][abr>=192]/bestaudio[abr>=192]/'
                    'bestaudio[ext=m4a]/bestaudio/best[height<=480]'
                )
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '320',  # Higher quality audio
                }]
            elif quality == '1080p':
                ydl_opts['format'] = (
                    'bestvideo[height<=1080][height>=1080]+bestaudio[ext=m4a]/'
                    'bestvideo[height<=1080][height>=1080]+bestaudio/'
                    'best[height<=1080][height>=1080]/'
                    'bestvideo[height<=1080]+bestaudio[ext=m4a]/'
                    'bestvideo[height<=1080]+bestaudio/'
                    'best[height<=1080]/best'
                )
            elif quality == '720p':
                ydl_opts['format'] = (
                    'bestvideo[height<=720][height>=720]+bestaudio[ext=m4a]/'
                    'bestvideo[height<=720][height>=720]+bestaudio/'
                    'best[height<=720][height>=720]/'
                    'bestvideo[height<=720]+bestaudio[ext=m4a]/'
                    'bestvideo[height<=720]+bestaudio/'
                    'best[height<=720]/best'
                )
            else:
                ydl_opts['format'] = 'best'
        
        # Remove the fallback that downgrades quality
        # Only use high-quality options
        ydl_opts['merge_output_format'] = 'mp4'  # Ensure good container format
        
        print(f"Starting download for {download_id} with quality {quality}")
        print(f"Format string: {ydl_opts['format']}")
        print(f"URL: {url}")
        
        # Download the video
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Update yt-dlp to latest version first
            try:
                ydl.download([url])
            except yt_dlp.utils.DownloadError as e:
                if "403" in str(e) or "Forbidden" in str(e):
                    # Try alternative approach for 403 errors - but maintain quality
                    print(f"403 error detected, trying alternative method with maintained quality...")
                    
                    # Keep the same quality format but change other settings
                    original_format = ydl_opts['format']
                    ydl_opts.update({
                        'user_agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'extractor_args': {'youtube': {'skip': ['dash'], 'player_skip': ['configs']}},
                        'format': original_format,  # Keep the same quality format
                    })
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl_retry:
                        ydl_retry.download([url])
                else:
                    raise e
        
        # Find the downloaded file
        downloaded_files = list(TEMP_DIR.glob(f"{download_id}_*"))
        if not downloaded_files:
            raise Exception("Downloaded file not found. Check if the video is available and not private/restricted.")
        
        downloaded_file = downloaded_files[0]
        print(f"Downloaded file: {downloaded_file}")
        
        # Move to downloads directory with a clean name
        final_filename = secure_filename(downloaded_file.name.replace(f"{download_id}_", ""))
        final_path = DOWNLOAD_DIR / final_filename
        
        # Ensure unique filename
        counter = 1
        while final_path.exists():
            name_parts = final_filename.rsplit('.', 1)
            if len(name_parts) == 2:
                final_filename = f"{name_parts[0]}_{counter}.{name_parts[1]}"
            else:
                final_filename = f"{final_filename}_{counter}"
            final_path = DOWNLOAD_DIR / final_filename
            counter += 1
        
        shutil.move(str(downloaded_file), str(final_path))
        print(f"File moved to: {final_path}")
        
        # Update progress
        progress_obj.status = "completed"
        progress_obj.progress = 100
        progress_obj.filename = final_filename
        progress_obj.file_path = final_path
        progress_obj.completed_at = datetime.now()
        
        print(f"Download completed successfully: {final_filename}")
        
    except Exception as e:
        error_msg = str(e)
        print(f"Download error for {download_id}: {error_msg}")
        
        # Provide more helpful error messages
        if "403" in error_msg or "Forbidden" in error_msg:
            error_msg = "YouTube blocked the download (403 Forbidden). This video may be restricted or require sign-in. Try a different video or update yt-dlp."
        elif "404" in error_msg or "not found" in error_msg.lower():
            error_msg = "Video not found (404). The video may be private, deleted, or the URL is incorrect."
        elif "copyright" in error_msg.lower():
            error_msg = "Video is protected by copyright and cannot be downloaded."
        elif "private" in error_msg.lower():
            error_msg = "This video is private and cannot be downloaded."
        elif "unavailable" in error_msg.lower():
            error_msg = "Video is unavailable in your region or has been removed."
        
        progress_obj.status = "error"
        progress_obj.error = error_msg
        
        # Cleanup any partial downloads
        for temp_file in TEMP_DIR.glob(f"{download_id}_*"):
            try:
                temp_file.unlink()
                print(f"Cleaned up temp file: {temp_file}")
            except:
                pass

@app.route('/api/info', methods=['POST'])
def get_info():
    """Get video information"""
    try:
        data = request.get_json()
        url = data.get('url')
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        
        info = get_video_info(url)
        return jsonify({'success': True, 'data': info})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
def start_download():
    """Start a video download"""
    try:
        data = request.get_json()
        url = data.get('url')
        quality = data.get('quality', 'best')
        custom_format = data.get('custom_format')  # Allow custom format specification
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        
        # Check concurrent download limit
        active_count = len([d for d in active_downloads.values() 
                          if d.status in ['starting', 'downloading', 'processing']])
        
        if active_count >= MAX_CONCURRENT_DOWNLOADS:
            return jsonify({'error': 'Too many concurrent downloads. Please wait.'}), 429
        
        # Generate unique download ID
        download_id = str(uuid.uuid4())
        
        # Create progress tracker
        with download_lock:
            active_downloads[download_id] = DownloadProgress(download_id)
        
        # Start download in background thread
        thread = threading.Thread(
            target=download_video,
            args=(url, quality, download_id, custom_format),
            daemon=True
        )
        thread.start()
        
        return jsonify({
            'success': True,
            'download_id': download_id,
            'message': 'Download started'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/progress/<download_id>', methods=['GET'])
def get_progress(download_id):
    """Get download progress"""
    if download_id not in active_downloads:
        return jsonify({'error': 'Download not found'}), 404
    
    progress_obj = active_downloads[download_id]
    
    return jsonify({
        'download_id': download_id,
        'status': progress_obj.status,
        'progress': progress_obj.progress,
        'filename': progress_obj.filename,
        'error': progress_obj.error,
        'completed_at': progress_obj.completed_at.isoformat() if progress_obj.completed_at else None
    })

@app.route('/api/download/<download_id>/file', methods=['GET'])
def download_file(download_id):
    """Download the completed file"""
    if download_id not in active_downloads:
        return jsonify({'error': 'Download not found'}), 404
    
    progress_obj = active_downloads[download_id]
    
    if progress_obj.status != 'completed' or not progress_obj.file_path:
        return jsonify({'error': 'File not ready for download'}), 400
    
    if not progress_obj.file_path.exists():
        return jsonify({'error': 'File not found'}), 404
    
    return send_file(
        progress_obj.file_path,
        as_attachment=True,
        download_name=progress_obj.filename
    )

@app.route('/api/downloads', methods=['GET'])
def list_downloads():
    """List all downloads"""
    downloads = []
    for download_id, progress_obj in active_downloads.items():
        downloads.append({
            'download_id': download_id,
            'status': progress_obj.status,
            'progress': progress_obj.progress,
            'filename': progress_obj.filename,
            'error': progress_obj.error,
            'completed_at': progress_obj.completed_at.isoformat() if progress_obj.completed_at else None
        })
    
    return jsonify({'downloads': downloads})

@app.route('/api/update-ytdlp', methods=['POST'])
def update_ytdlp():
    """Update yt-dlp to the latest version"""
    try:
        import subprocess
        import sys
        
        # Update yt-dlp
        result = subprocess.run([
            sys.executable, '-m', 'pip', 'install', '--upgrade', 'yt-dlp'
        ], capture_output=True, text=True)
        
        if result.returncode == 0:
            return jsonify({
                'success': True,
                'message': 'yt-dlp updated successfully',
                'output': result.stdout
            })
        else:
            return jsonify({
                'success': False,
                'error': result.stderr,
                'message': 'Failed to update yt-dlp'
            }), 500
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/formats', methods=['POST'])
def get_available_formats():
    """Get all available formats for a video"""
    try:
        data = request.get_json()
        url = data.get('url')
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'listformats': True,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            formats = []
            if 'formats' in info:
                for f in info['formats']:
                    if f.get('vcodec') != 'none' or f.get('acodec') != 'none':
                        format_info = {
                            'format_id': f.get('format_id'),
                            'ext': f.get('ext'),
                            'resolution': f.get('resolution', 'N/A'),
                            'width': f.get('width'),
                            'height': f.get('height'),
                            'fps': f.get('fps'),
                            'vcodec': f.get('vcodec'),
                            'acodec': f.get('acodec'),
                            'abr': f.get('abr'),  # Audio bitrate
                            'vbr': f.get('vbr'),  # Video bitrate
                            'tbr': f.get('tbr'),  # Total bitrate
                            'filesize': f.get('filesize'),
                            'filesize_approx': f.get('filesize_approx'),
                            'format_note': f.get('format_note'),
                            'quality': f.get('quality'),
                        }
                        formats.append(format_info)
            
            # Sort by quality (height) descending
            formats.sort(key=lambda x: x.get('height', 0) if x.get('height') else 0, reverse=True)
            
            return jsonify({
                'success': True,
                'title': info.get('title'),
                'formats': formats,
                'recommended': {
                    'best_video': next((f for f in formats if f.get('height', 0) >= 1080 and f.get('vcodec') != 'none'), None),
                    'best_audio': next((f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none'), None),
                }
            })
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500
def test_url():
    """Test if a URL is accessible without downloading"""
    try:
        data = request.get_json()
        url = data.get('url')
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        
        # Test with minimal options
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'simulate': True,  # Don't download, just test
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                'success': True,
                'accessible': True,
                'title': info.get('title', 'Unknown'),
                'message': 'URL is accessible'
            })
            
    except Exception as e:
        error_msg = str(e)
        accessible = False
        
        if "403" in error_msg:
            message = "URL is blocked (403 Forbidden)"
        elif "404" in error_msg:
            message = "Video not found (404)"
        elif "private" in error_msg.lower():
            message = "Video is private"
        else:
            message = f"Error: {error_msg}"
            
        return jsonify({
            'success': True,
            'accessible': accessible,
            'message': message,
            'error': error_msg
        })
    """Clean up old downloads"""
    try:
        cutoff_time = datetime.now() - timedelta(hours=MAX_FILE_AGE_HOURS)
        cleaned_count = 0
        
        # Clean up completed downloads
        to_remove = []
        for download_id, progress_obj in active_downloads.items():
            if (progress_obj.completed_at and 
                progress_obj.completed_at < cutoff_time):
                # Remove file if it exists
                if progress_obj.file_path and progress_obj.file_path.exists():
                    try:
                        progress_obj.file_path.unlink()
                        cleaned_count += 1
                    except:
                        pass
                to_remove.append(download_id)
        
        # Remove from active downloads
        for download_id in to_remove:
            del active_downloads[download_id]
        
        # Clean up any orphaned files in download directory
        for file_path in DOWNLOAD_DIR.iterdir():
            if file_path.is_file():
                file_age = datetime.now() - datetime.fromtimestamp(file_path.stat().st_mtime)
                if file_age > timedelta(hours=MAX_FILE_AGE_HOURS):
                    try:
                        file_path.unlink()
                        cleaned_count += 1
                    except:
                        pass
        
        # Clean up temp directory
        for file_path in TEMP_DIR.iterdir():
            try:
                file_path.unlink()
            except:
                pass
        
        return jsonify({
            'success': True,
            'cleaned_files': cleaned_count,
            'message': f'Cleaned up {cleaned_count} old files'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'active_downloads': len(active_downloads),
        'download_dir': str(DOWNLOAD_DIR.absolute()),
        'temp_dir': str(TEMP_DIR.absolute())
    })

if __name__ == '__main__':
    print("🎬 YouTube Downloader Backend Starting...")
    print(f"📁 Downloads will be saved to: {DOWNLOAD_DIR.absolute()}")
    print(f"🔧 Temp directory: {TEMP_DIR.absolute()}")
    print(f"🌐 API will be available at: http://localhost:5000")
    print("📋 API Endpoints:")
    print("  POST /api/info - Get video information")
    print("  POST /api/download - Start download")
    print("  GET  /api/progress/<id> - Check progress")
    print("  GET  /api/download/<id>/file - Download file")
    print("  GET  /api/downloads - List all downloads")
    print("  POST /api/cleanup - Clean old files")
    print("  POST /api/update-ytdlp - Update yt-dlp")
    print("  POST /api/test-url - Test URL accessibility")
    print("  GET  /health - Health check")
    
    # Auto-cleanup on startup
    try:
        # Clean temp directory
        for temp_file in TEMP_DIR.glob("*"):
            temp_file.unlink()
        print("🧹 Cleaned temp directory on startup")
    except:
        pass
    
    app.run(debug=True, host='0.0.0.0', port=5000)