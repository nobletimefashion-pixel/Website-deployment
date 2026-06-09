# Tools/builtin/video_creator.py
import os
import json
import asyncio
import re
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Literal
import requests
import urllib.parse

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult
from nexus_agent.config.config import Config
from nexus_agent.utils.path import resolve_path


class VideoCreatorParams(BaseModel):
    action: Literal[
        "create_video",
        "create_story_video",
        "create_quote_video",
        "create_tutorial",
        "create_slideshow",
        "create_batch"
    ] = Field(..., description="Type of video to create")
    
    topic: str = Field(..., description="Topic for the video")
    duration: int = Field(60, ge=15, le=300, description="Duration in seconds")
    style: Literal["documentary", "casual", "educational", "motivational", "news", "cinematic"] = Field("motivational", description="Video style")
    output_path: str = Field("output_video.mp4", description="Output file path")
    resolution: Literal["720p", "1080p"] = Field("1080p", description="Video resolution")
    num_images: int = Field(10, ge=3, le=30, description="Number of images")
    voice: str | None = Field(None, description="Voice (auto-selected if None)")
    include_music: bool = Field(True, description="Include background music")
    add_captions: bool = Field(True, description="Add captions")
    use_whisper: bool = Field(False, description="Use Whisper for perfect caption sync")
    template: str | None = Field(None, description="Visual template (dark/light/vibrant)")
    fps: int = Field(30, description="Frames per second")
    batch_topics: list[str] | None = Field(None, description="List of topics for batch processing")


class VideoCreatorTool(Tool):
    name = "video_creator"
    description = (
        "Professional Instagram/TikTok video creator with AI scripts, real image downloads, "
        "Whisper-synced captions, music, and templates. Creates viral-ready motivational content."
    )
    kind = ToolKind.WRITE
    schema = VideoCreatorParams
    
    RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080)}
    
    VOICE_PROFILES = {
        "motivational": ["en-US-GuyNeural", "Deep, authoritative"],
        "cinematic": ["en-US-GuyNeural", "Dramatic narrative"],
        "documentary": ["en-GB-RyanNeural", "Professional"],
        "news": ["en-US-JasonNeural", "Clear, authoritative"],
        "educational": ["en-US-AriaNeural", "Clear, friendly"],
        "casual": ["en-US-JennyNeural", "Conversational"]
    }
    
    # Free music URLs (Creative Commons)
    MUSIC_LIBRARY = {
        "motivational": "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/WFMU/Kai_Engel/Chapter_Four__Reasons/Kai_Engel_-_13_-_The_Purpose.mp3",
        "cinematic": "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Kevin_MacLeod/Impact/Kevin_MacLeod_-_Killers.mp3",
        "upbeat": "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/ccCommunity/Tours/Enthusiast/Tours_-_01_-_Enthusiast.mp3"
    }
    
    TEMPLATES = {
        "dark": {"bg": (15, 15, 20), "text": (255, 215, 0), "accent": (218, 165, 32)},
        "light": {"bg": (245, 245, 250), "text": (30, 30, 40), "accent": (70, 130, 180)},
        "vibrant": {"bg": (20, 20, 40), "text": (255, 255, 255), "accent": (255, 50, 100)}
    }
    
    def __init__(self, config: Config):
        super().__init__(config)
        self._temp_dir = Path.home() / ".nexus-agent" / "video_temp"
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._assets_dir = Path.home() / ".nexus-agent" / "video_assets"
        self._assets_dir.mkdir(parents=True, exist_ok=True)
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = VideoCreatorParams(**invocation.params)
        
        if not params.voice:
            params.voice = self.VOICE_PROFILES.get(params.style, self.VOICE_PROFILES["motivational"])[0]
        
        # Batch processing
        if params.action == "create_batch" and params.batch_topics:
            return await self._create_batch(params, invocation.cwd)
        
        # Single video
        if params.action == "create_story_video":
            params.style = "cinematic"
        elif params.action == "create_quote_video":
            params.style = "motivational"
            params.duration = min(params.duration, 60)
            params.num_images = 5
        
        return await self._create_video(params, invocation.cwd)
    
    async def _create_video(self, params: VideoCreatorParams, cwd: Path) -> ToolResult:
        """Main video creation pipeline with ALL improvements."""
        try:
            output_path = resolve_path(cwd, params.output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            width, height = self.RESOLUTIONS[params.resolution]
            
            print(f"\n🎬 Creating {params.style} video: {params.topic}")
            print(f"📐 {params.resolution} ({width}x{height}), {params.duration}s")
            print(f"🎙️  Voice: {params.voice}")
            if params.template:
                print(f"🎨 Template: {params.template}")
            
            # Step 1: Generate script
            print("\n📝 Generating script...")
            script_data = await self._generate_script_robust(params)
            
            if not script_data:
                return ToolResult.error_result("Failed to generate script")
            
            print(f"✓ Script: {len(script_data['narration'].split())} words")
            
            # Step 2: Download images (IMPROVED - multiple sources)
            print("\n🖼️  Downloading images (Pexels → Pixabay → Wikimedia → AI Gen)...")
            images = await self._download_images_advanced(script_data['scenes'], params.topic)
            
            if not images:
                return ToolResult.error_result("Failed to get images")
            
            print(f"✓ Got {len(images)} images")
            
            # Step 3: Generate voiceover
            print("\n🎙️  Generating voiceover...")
            audio_path = await self._generate_voiceover(script_data['narration'], params.voice)
            
            if not audio_path:
                return ToolResult.error_result("Failed to generate voiceover")
            
            print(f"✓ Voiceover ready")
            
            # Step 4: Download REAL music
            music_path = None
            if params.include_music:
                print("\n🎵 Downloading background music...")
                music_path = await self._download_real_music(params.style)
                print(f"✓ Music ready")
            
            # Step 5: Generate captions (with optional Whisper)
            captions = None
            if params.add_captions:
                if params.use_whisper:
                    print("\n💬 Generating Whisper-synced captions...")
                    captions = await self._generate_whisper_captions(audio_path)
                else:
                    print("\n💬 Generating captions...")
                    captions = await self._generate_captions(audio_path, script_data['narration'])
                
                print(f"✓ {len(captions)} captions")
            
            # Step 6: Assemble with template
            print("\n🎬 Assembling video...")
            success = await self._assemble_video_advanced(
                images, audio_path, music_path, captions, output_path,
                width, height, params.fps, params.style, params.template
            )
            
            if not success:
                return ToolResult.error_result("Failed to assemble video")
            
            print("✓ Complete!\n")
            
            self._cleanup()
            
            size_mb = output_path.stat().st_size / 1_000_000 if output_path.exists() else 0
            
            return ToolResult.success_result(
                output=f"✅ Professional video created!\n📁 {output_path}\n📊 {size_mb:.1f} MB\n🎨 {params.style} style",
                metadata={"output_path": str(output_path), "topic": params.topic}
            )
        
        except Exception as e:
            import traceback
            return ToolResult.error_result(f"Error: {str(e)}\n{traceback.format_exc()}")
    
    async def _create_batch(self, params: VideoCreatorParams, cwd: Path) -> ToolResult:
        """Batch process multiple videos."""
        results = []
        
        print(f"\n🎬 Batch creating {len(params.batch_topics)} videos...")
        
        for i, topic in enumerate(params.batch_topics, 1):
            print(f"\n{'='*60}")
            print(f"Video {i}/{len(params.batch_topics)}: {topic}")
            print(f"{'='*60}")
            
            # Create individual video
            batch_params = VideoCreatorParams(
                action="create_video",
                topic=topic,
                duration=params.duration,
                style=params.style,
                output_path=f"batch_video_{i}_{topic[:20].replace(' ', '_')}.mp4",
                resolution=params.resolution,
                num_images=params.num_images,
                voice=params.voice,
                include_music=params.include_music,
                add_captions=params.add_captions,
                use_whisper=params.use_whisper,
                template=params.template,
                fps=params.fps
            )
            
            result = await self._create_video(batch_params, cwd)
            results.append((topic, result.success))
        
        # Summary
        success_count = sum(1 for _, success in results if success)
        
        summary = f"\n✅ Batch complete: {success_count}/{len(params.batch_topics)} videos created\n\n"
        for topic, success in results:
            summary += f"{'✓' if success else '✗'} {topic}\n"
        
        return ToolResult.success_result(output=summary)
    
    async def _generate_script_robust(self, params: VideoCreatorParams) -> dict | None:
        """Generate script with fallback."""
        try:
            from client.llm_client import LLMClient
            
            client = LLMClient(config=self.config)
            target_words = int((params.duration / 60) * 150)
            
            prompt = f"""Create a powerful {params.style} video script about: {params.topic}

Requirements:
- {target_words} words for {params.duration} seconds
- {params.num_images} scenes with specific image queries
- Make it VIRAL-WORTHY and engaging

Return ONLY valid JSON:
{{
  "narration": "Full powerful script...",
  "scenes": [
    {{"scene_number": 1, "image_query": "Alexander Great marble statue ancient", "text": "snippet"}}
  ]
}}

For historical figures: use "name statue/bust/sculpture ancient art"
For concepts: use "concept visualization artistic representation"
"""
            
            full_content = ""
            async for event in client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                stream=True
            ):
                if event.text_delta and event.text_delta.content:
                    full_content += event.text_delta.content
            
            if not full_content:
                return self._create_fallback_script(params)
            
            # Extract JSON
            content = re.sub(r'```json\s*|```\s*', '', full_content.strip())
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                content = json_match.group(0)
            
            try:
                script_data = json.loads(content)
                if "narration" in script_data and "scenes" in script_data:
                    return script_data
            except:
                pass
            
            return self._create_fallback_script(params)
        
        except Exception as e:
            print(f"⚠️  Script error: {e}")
            return self._create_fallback_script(params)
    
    def _create_fallback_script(self, params: VideoCreatorParams) -> dict:
        """Fallback script generator."""
        print("📝 Using fallback script...")
        
        if "alexander" in params.topic.lower():
            narration = (
                "Alexander the Great. Born in 356 BC in Macedonia. "
                "Tutored by Aristotle. King at 20. Conquered the known world by 32. "
                "Never lost a battle. Founded over 20 cities. "
                "His empire stretched from Greece to India. "
                "Led from the front. Brilliant strategist. Fearless warrior. "
                "His legacy endured for centuries. "
                "Greatness isn't inherited. It's earned through action."
            )
            queries = [
                "Alexander Great marble statue ancient",
                "Macedonia ancient Greece palace",
                "Greek phalanx army battle",
                "Aristotle teaching philosophy",
                "Persian empire ancient map",
                "Ancient cavalry warfare",
                "Alexandria Egypt library",
                "Hellenistic art culture",
                "Ancient battle scene",
                "Alexander portrait mosaic"
            ]
        else:
            narration = (
                f"Let's talk about {params.topic}. "
                "Greatness is achieved by those who dare to act. "
                "Every legend started as an ordinary person with extraordinary vision. "
                "They faced obstacles. They persisted. "
                "Success is about relentless determination. "
                "The world remembers those who took action."
            )
            queries = [f"{params.topic} historical art {i}" for i in range(1, 11)]
        
        sentences = [s.strip() for s in narration.split('.') if s.strip()]
        scenes = []
        
        for i in range(params.num_images):
            scenes.append({
                "scene_number": i + 1,
                "image_query": queries[i] if i < len(queries) else queries[-1],
                "text": sentences[i] if i < len(sentences) else sentences[-1]
            })
        
        return {"narration": narration, "scenes": scenes}
    
    async def _download_images_advanced(self, scenes: list[dict], topic: str) -> list[Path]:
        """Multi-source image download with AI generation fallback."""
        images = []
        
        for i, scene in enumerate(scenes):
            query = scene.get('image_query', topic)
            print(f"  Scene {i+1}: {query[:45]}...")
            
            image_path = None
            
            # Try 1: Pexels (best for general content)
            image_path = await self._download_pexels(query, i)
            
            # Try 2: Pixabay (good for illustrations)
            if not image_path:
                image_path = await self._download_pixabay(query, i)
            
            # Try 3: Wikimedia (best for historical)
            if not image_path:
                image_path = await self._download_wikimedia(query, i)
            
            # Try 4: AI Generation (if available)
            if not image_path:
                image_path = await self._generate_ai_image(query, i)
            
            # Final: Artistic placeholder
            if not image_path:
                print(f"    → Template placeholder")
                image_path = self._create_template_placeholder(query, topic, i)
            else:
                print(f"    ✓ Downloaded")
            
            if image_path and image_path.exists():
                images.append(image_path)
        
        return images
    
    async def _download_pexels(self, query: str, index: int) -> Path | None:
        """Download from Pexels (requires free API key - add yours!)."""
        try:
            # Get free API key from: https://www.pexels.com/api/
            api_key = os.getenv("PEXELS_API_KEY")
            if not api_key:
                return None
            
            image_path = self._temp_dir / f"image_{index}.jpg"
            
            url = f"https://api.pexels.com/v1/search?query={urllib.parse.quote(query)}&per_page=1"
            headers = {"Authorization": api_key}
            
            response = requests.get(url, headers=headers, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("photos"):
                    img_url = data["photos"][0]["src"]["large"]
                    
                    img_response = requests.get(img_url, timeout=20)
                    if img_response.status_code == 200:
                        with open(image_path, 'wb') as f:
                            f.write(img_response.content)
                        
                        from PIL import Image
                        img = Image.open(image_path)
                        img.verify()
                        return image_path
        except Exception as e:
            print(f"    Pexels: {e}")
        
        return None
    
    async def _download_pixabay(self, query: str, index: int) -> Path | None:
        """Download from Pixabay (requires free API key)."""
        try:
            # Get free key from: https://pixabay.com/api/docs/
            api_key = os.getenv("PIXABAY_API_KEY")
            if not api_key:
                return None
            
            image_path = self._temp_dir / f"image_{index}.jpg"
            
            url = f"https://pixabay.com/api/?key={api_key}&q={urllib.parse.quote(query)}&image_type=photo&per_page=3"
            
            response = requests.get(url, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("hits"):
                    img_url = data["hits"][0]["largeImageURL"]
                    
                    img_response = requests.get(img_url, timeout=20)
                    if img_response.status_code == 200:
                        with open(image_path, 'wb') as f:
                            f.write(img_response.content)
                        
                        from PIL import Image
                        img = Image.open(image_path)
                        img.verify()
                        return image_path
        except Exception as e:
            print(f"    Pixabay: {e}")
        
        return None
    
    async def _download_wikimedia(self, query: str, index: int) -> Path | None:
        """Download from Wikimedia Commons."""
        try:
            image_path = self._temp_dir / f"image_{index}.jpg"
            
            url = "https://commons.wikimedia.org/w/api.php"
            params = {
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": query,
                "gsrlimit": 3,
                "prop": "imageinfo",
                "iiprop": "url",
                "iiurlwidth": 1920
            }
            
            response = requests.get(url, params=params, timeout=15)
            data = response.json()
            
            if "query" in data and "pages" in data["query"]:
                for page in data["query"]["pages"].values():
                    if "imageinfo" in page and page["imageinfo"]:
                        img_url = page["imageinfo"][0].get("thumburl") or page["imageinfo"][0].get("url")
                        
                        if img_url:
                            img_response = requests.get(img_url, timeout=20)
                            
                            if img_response.status_code == 200 and len(img_response.content) > 5000:
                                with open(image_path, 'wb') as f:
                                    f.write(img_response.content)
                                
                                from PIL import Image
                                img = Image.open(image_path)
                                img.verify()
                                img = Image.open(image_path)
                                img.load()
                                return image_path
        except Exception as e:
            print(f"    Wikimedia: {e}")
        
        return None
    
    async def _generate_ai_image(self, query: str, index: int) -> Path | None:
        """Generate image using AI (Stability AI / DALL-E if API key available)."""
        # Placeholder for AI image generation
        # Add your Stability AI or DALL-E API key here
        return None
    
    def _create_template_placeholder(self, query: str, topic: str, index: int) -> Path:
        """Create placeholder with template styling."""
        from PIL import Image, ImageDraw, ImageFont
        
        output_path = self._temp_dir / f"image_{index}.jpg"
        
        # Use template colors or default
        template = self.TEMPLATES.get("dark", self.TEMPLATES["dark"])
        
        img = Image.new('RGB', (1920, 1080), color=template["bg"])
        draw = ImageDraw.Draw(img)
        
        # Gradient overlay
        for y in range(1080):
            alpha = int(30 * (1 - y / 1080))
            r, g, b = template["bg"]
            draw.line([(0, y), (1920, y)], fill=(r, g, b + alpha))
        
        # Load font
        font = None
        for font_path in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arial.ttf"]:
            try:
                font = ImageFont.truetype(font_path, 85)
                break
            except:
                continue
        
        if not font:
            font = ImageFont.load_default()
        
        # Draw text
        text = topic.upper()[:28]
        
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            x = (1920 - (bbox[2] - bbox[0])) // 2
            y = (1080 - (bbox[3] - bbox[1])) // 2
            
            # Shadow
            draw.text((x + 5, y + 5), text, fill=(0, 0, 0), font=font)
            # Main
            draw.text((x, y), text, fill=template["accent"], font=font)
        except:
            draw.text((200, 450), text, fill=template["accent"], font=font)
        
        img.save(output_path, quality=95)
        return output_path
    
    async def _generate_voiceover(self, text: str, voice: str) -> Path | None:
        """Generate TTS voiceover."""
        try:
            import edge_tts
            
            text = re.sub(r'\[.*?\]', '', text)
            text = re.sub(r'\s+', ' ', text).strip()
            
            audio_path = self._temp_dir / "voiceover.mp3"
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(audio_path))
            
            return audio_path
        except Exception as e:
            print(f"⚠️  Voiceover: {e}")
            return None
    
    async def _download_real_music(self, style: str) -> Path | None:
        """Download real Creative Commons music."""
        try:
            music_type = "motivational" if style in ["motivational", "cinematic"] else "upbeat"
            url = self.MUSIC_LIBRARY.get(music_type)
            
            if not url:
                return None
            
            music_path = self._assets_dir / f"{music_type}.mp3"
            
            if not music_path.exists():
                print(f"      Downloading {music_type} music...")
                response = requests.get(url, timeout=30)
                
                if response.status_code == 200:
                    with open(music_path, 'wb') as f:
                        f.write(response.content)
            
            return music_path if music_path.exists() else None
        except Exception as e:
            print(f"⚠️  Music: {e}")
            return None
    
    async def _generate_whisper_captions(self, audio_path: Path) -> list[dict]:
        """Generate perfect word-level captions using Whisper."""
        try:
            import whisper
            
            print("      Loading Whisper model...")
            model = whisper.load_model("base")
            
            print("      Transcribing...")
            result = model.transcribe(str(audio_path), word_timestamps=True, language="en")
            
            captions = []
            for segment in result.get("segments", []):
                if "words" in segment:
                    for word_data in segment["words"]:
                        captions.append({
                            "start": word_data["start"],
                            "end": word_data["end"],
                            "text": word_data["word"].strip().upper()
                        })
            
            # Group into 3-word phrases
            grouped = []
            for i in range(0, len(captions), 3):
                group = captions[i:i+3]
                if group:
                    grouped.append({
                        "start": group[0]["start"],
                        "end": group[-1]["end"],
                        "text": " ".join(w["text"] for w in group)
                    })
            
            return grouped
        except Exception as e:
            print(f"⚠️  Whisper failed: {e}")
            return []
    
    async def _generate_captions(self, audio_path: Path, script: str) -> list[dict]:
        """Generate time-based captions."""
        try:
            import moviepy.editor as mpe
            
            audio = mpe.AudioFileClip(str(audio_path))
            duration = audio.duration
            audio.close()
            
            words = script.split()
            words_per_second = len(words) / duration
            
            captions = []
            current_time = 0
            words_per_caption = 3
            
            for i in range(0, len(words), words_per_caption):
                caption_words = words[i:i + words_per_caption]
                caption_duration = len(caption_words) / words_per_second
                
                captions.append({
                    "start": current_time,
                    "end": current_time + caption_duration,
                    "text": " ".join(caption_words).upper()
                })
                
                current_time += caption_duration
            
            return captions
        except:
            return []
    
    async def _assemble_video_advanced(
        self, images: list[Path], audio_path: Path, music_path: Path | None,
        captions: list[dict], output_path: Path, width: int, height: int,
        fps: int, style: str, template: str | None
    ) -> bool:
        """Assemble with advanced effects and music."""
        try:
            import moviepy.editor as mpe
            import numpy as np
            
            audio = mpe.AudioFileClip(str(audio_path))
            total_duration = audio.duration
            duration_per_image = total_duration / len(images)
            
            # Create clips with Ken Burns
            video_clips = []
            
            for i, image_path in enumerate(images):
                if not image_path.exists():
                    continue
                
                clip = mpe.ImageClip(str(image_path)).set_duration(duration_per_image)
                clip = clip.resize(newsize=(width, height))
                
                # Ken Burns zoom
                if i % 2 == 0:
                    clip = clip.resize(lambda t: 1 + 0.10 * (t / duration_per_image))
                else:
                    clip = clip.resize(lambda t: 1.10 - 0.10 * (t / duration_per_image))
                
                clip = clip.resize(newsize=(width, height))
                
                # Cinematic vignette
                if style in ["cinematic", "motivational"]:
                    def add_vignette(frame):
                        h, w = frame.shape[:2]
                        X, Y = np.meshgrid(np.linspace(-1, 1, w), np.linspace(-1, 1, h))
                        vignette = 1 - (X**2 + Y**2) * 0.3
                        vignette = np.clip(vignette, 0.7, 1)
                        return (frame * vignette[:, :, np.newaxis]).astype('uint8')
                    
                    clip = clip.fl_image(add_vignette)
                
                video_clips.append(clip)
            
            if not video_clips:
                return False
            
            # Concatenate with crossfades
            if len(video_clips) > 1:
                final_clips = [video_clips[0]]
                
                for i in range(1, len(video_clips)):
                    video_clips[i] = video_clips[i].crossfadein(0.8)
                    final_clips.append(video_clips[i].set_start(
                        sum(c.duration for c in final_clips) - 0.8
                    ))
                
                video = mpe.CompositeVideoClip(final_clips)
            else:
                video = video_clips[0]
            
            # Add music
            if music_path and music_path.exists():
                music = mpe.AudioFileClip(str(music_path))
                
                if music.duration < total_duration:
                    music = music.audio_loop(duration=total_duration)
                else:
                    music = music.subclip(0, total_duration)
                
                music = music.volumex(0.15)  # Quiet background
                
                from moviepy.audio.AudioClip import CompositeAudioClip
                final_audio = CompositeAudioClip([audio, music])
            else:
                final_audio = audio
            
            video = video.set_audio(final_audio)
            
            # Add captions
            if captions:
                caption_clips = [video]
                
                for caption in captions:
                    try:
                        txt_clip = mpe.TextClip(
                            caption['text'],
                            fontsize=80,
                            color='white',
                            font='DejaVu-Sans-Bold',
                            stroke_color='black',
                            stroke_width=6,
                            method='caption',
                            size=(int(width * 0.9), None),
                            align='center'
                        )
                        
                        txt_clip = txt_clip.set_position(('center', height - 180))
                        txt_clip = txt_clip.set_start(caption['start'])
                        txt_clip = txt_clip.set_duration(caption['end'] - caption['start'])
                        txt_clip = txt_clip.crossfadein(0.15).crossfadeout(0.15)
                        
                        caption_clips.append(txt_clip)
                    except Exception as e:
                        print(f"⚠️  Caption: {e}")
                        continue
                
                video = mpe.CompositeVideoClip(caption_clips)
            
            # Render
            video.write_videofile(
                str(output_path),
                fps=fps,
                codec='libx264',
                audio_codec='aac',
                bitrate='8000k',
                threads=4,
                preset='medium',
                logger=None
            )
            
            audio.close()
            return True
        
        except Exception as e:
            import traceback
            print(f"⚠️  Assembly: {e}")
            print(traceback.format_exc())
            return False
    
    def _cleanup(self):
        """Cleanup temp files."""
        try:
            for file in self._temp_dir.glob("*"):
                if file.is_file() and file.name != "music.mp3":
                    try:
                        file.unlink()
                    except:
                        pass
        except:
            pass
    
    # Specialized methods
    async def _create_story_video(self, params: VideoCreatorParams, cwd: Path) -> ToolResult:
        params.style = "cinematic"
        if not params.voice:
            params.voice = "en-US-GuyNeural"
        return await self._create_video(params, cwd)
    
    async def _create_quote_video(self, params: VideoCreatorParams, cwd: Path) -> ToolResult:
        params.style = "motivational"
        params.duration = min(params.duration, 60)
        params.num_images = 5
        if not params.voice:
            params.voice = "en-US-GuyNeural"
        return await self._create_video(params, cwd)
    
    async def _create_tutorial(self, params: VideoCreatorParams, cwd: Path) -> ToolResult:
        params.style = "educational"
        return await self._create_video(params, cwd)
    
    async def _create_slideshow(self, params: VideoCreatorParams, cwd: Path) -> ToolResult:
        return await self._create_video(params, cwd)