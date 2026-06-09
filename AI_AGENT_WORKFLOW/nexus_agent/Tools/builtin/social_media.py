# Tools/builtin/social_media.py
import os
import json
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Literal, Optional

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult
from nexus_agent.utils.path import resolve_path


class SocialMediaParams(BaseModel):
    action: Literal[
        "post_video",
        "post_image",
        "post_text",
        "schedule_post",
        "get_analytics",
        "delete_post"
    ] = Field(
        ...,
        description="Action to perform"
    )
    
    platform: Literal[
        "tiktok",
        "instagram",
        "youtube",
        "twitter",
        "facebook",
        "linkedin",
        "all"
    ] = Field(
        ...,
        description="Social media platform to post to ('all' posts to all configured platforms)"
    )
    
    # Content paths
    media_path: str | None = Field(
        None,
        description="Path to video or image file to upload"
    )
    
    # Post content
    caption: str | None = Field(
        None,
        description="Caption/description for the post"
    )
    title: str | None = Field(
        None,
        description="Title for the post (required for YouTube)"
    )
    tags: list[str] | None = Field(
        None,
        description="Hashtags/tags for the post (without # symbol)"
    )
    
    # Post settings
    visibility: Literal["public", "private", "unlisted"] | None = Field(
        "public",
        description="Post visibility (default: public)"
    )
    allow_comments: bool = Field(
        True,
        description="Allow comments on the post (default: True)"
    )
    allow_duet: bool | None = Field(
        True,
        description="Allow duets/remixes (TikTok/Instagram specific)"
    )
    
    # Scheduling
    schedule_time: str | None = Field(
        None,
        description="Schedule post for later (ISO format: 2024-01-15T10:00:00)"
    )
    
    # Analytics
    post_id: str | None = Field(
        None,
        description="Post ID for analytics or deletion"
    )
    
    # API credentials (optional - can use env vars)
    api_credentials: dict | None = Field(
        None,
        description="Platform-specific API credentials"
    )


class SocialMediaTool(Tool):
    name = "social_media"
    description = (
        "Post content to social media platforms (TikTok, Instagram, YouTube, Twitter, Facebook, LinkedIn). "
        "Can upload videos, images, schedule posts, and get analytics. "
        "Requires platform-specific API credentials in environment variables or config file."
    )
    kind = ToolKind.WRITE
    schema = SocialMediaParams
    
    REQUIRED_ENV_VARS = {
        "tiktok": ["TIKTOK_ACCESS_TOKEN"],
        "instagram": ["INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_BUSINESS_ACCOUNT_ID"],
        "youtube": ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"],
        "twitter": ["TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET"],
        "facebook": ["FACEBOOK_ACCESS_TOKEN", "FACEBOOK_PAGE_ID"],
        "linkedin": ["LINKEDIN_ACCESS_TOKEN", "LINKEDIN_PERSON_URN"]
    }
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = SocialMediaParams(**invocation.params)
        
        # Resolve media path if provided
        media_path = None
        if params.media_path:
            media_path = resolve_path(invocation.cwd, params.media_path)
            if not media_path.exists():
                return ToolResult.error_result(
                    f"Media file not found: {media_path}",
                    metadata={"media_path": str(media_path)}
                )
        
        # Route to appropriate handler
        if params.action == "post_video":
            return await self._post_video(params, media_path, invocation.cwd)
        elif params.action == "post_image":
            return await self._post_image(params, media_path, invocation.cwd)
        elif params.action == "post_text":
            return await self._post_text(params, invocation.cwd)
        elif params.action == "schedule_post":
            return await self._schedule_post(params, media_path, invocation.cwd)
        elif params.action == "get_analytics":
            return await self._get_analytics(params, invocation.cwd)
        elif params.action == "delete_post":
            return await self._delete_post(params, invocation.cwd)
        
        return ToolResult.error_result(f"Unknown action: {params.action}")
    
    def _check_credentials(self, platform: str) -> tuple[bool, str]:
        """Check if required credentials are available for platform."""
        if platform == "all":
            return True, ""
        
        required_vars = self.REQUIRED_ENV_VARS.get(platform, [])
        missing = [var for var in required_vars if not os.environ.get(var)]
        
        if missing:
            return False, f"Missing environment variables for {platform}: {', '.join(missing)}"
        return True, ""
    
    async def _post_video(self, params: SocialMediaParams, media_path: Path, cwd: Path) -> ToolResult:
        """Post a video to social media platform(s)."""
        if not media_path:
            return ToolResult.error_result("media_path is required for post_video action")
        
        if not media_path.suffix.lower() in ['.mp4', '.mov', '.avi', '.mkv']:
            return ToolResult.error_result(
                f"Invalid video format: {media_path.suffix}. Supported: .mp4, .mov, .avi, .mkv"
            )
        
        platforms = self._get_platforms(params.platform)
        results = {}
        errors = []
        
        for platform in platforms:
            has_creds, error_msg = self._check_credentials(platform)
            if not has_creds:
                errors.append(error_msg)
                continue
            
            try:
                if platform == "tiktok":
                    result = self._post_to_tiktok(media_path, params)
                elif platform == "instagram":
                    result = self._post_to_instagram(media_path, params, is_video=True)
                elif platform == "youtube":
                    result = self._post_to_youtube(media_path, params)
                elif platform == "twitter":
                    result = self._post_to_twitter(media_path, params, is_video=True)
                elif platform == "facebook":
                    result = self._post_to_facebook(media_path, params, is_video=True)
                else:
                    result = {"success": False, "error": f"Video posting not supported for {platform}"}
                
                results[platform] = result
            except Exception as e:
                errors.append(f"{platform}: {str(e)}")
                results[platform] = {"success": False, "error": str(e)}
        
        # Build output message
        successful = [p for p, r in results.items() if r.get("success")]
        failed = [p for p, r in results.items() if not r.get("success")]
        
        output_lines = [f"Posted video to {len(successful)}/{len(platforms)} platform(s)\n"]
        
        if successful:
            output_lines.append("✅ Successful:")
            for platform in successful:
                post_url = results[platform].get("url", "N/A")
                post_id = results[platform].get("post_id", "N/A")
                output_lines.append(f"  - {platform.upper()}: {post_url} (ID: {post_id})")
        
        if failed or errors:
            output_lines.append("\n❌ Failed:")
            for platform in failed:
                output_lines.append(f"  - {platform.upper()}: {results[platform].get('error', 'Unknown error')}")
            for error in errors:
                output_lines.append(f"  - {error}")
        
        output = "\n".join(output_lines)
        
        if successful:
            return ToolResult.success_result(
                output=output,
                metadata={
                    "successful_platforms": successful,
                    "failed_platforms": failed,
                    "results": results
                }
            )
        else:
            return ToolResult.error_result(
                output,
                metadata={"results": results}
            )
    
    async def _post_image(self, params: SocialMediaParams, media_path: Path, cwd: Path) -> ToolResult:
        """Post an image to social media platform(s)."""
        if not media_path:
            return ToolResult.error_result("media_path is required for post_image action")
        
        if not media_path.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
            return ToolResult.error_result(
                f"Invalid image format: {media_path.suffix}. Supported: .jpg, .jpeg, .png, .gif, .webp"
            )
        
        platforms = self._get_platforms(params.platform)
        results = {}
        errors = []
        
        for platform in platforms:
            has_creds, error_msg = self._check_credentials(platform)
            if not has_creds:
                errors.append(error_msg)
                continue
            
            try:
                if platform == "instagram":
                    result = self._post_to_instagram(media_path, params, is_video=False)
                elif platform == "twitter":
                    result = self._post_to_twitter(media_path, params, is_video=False)
                elif platform == "facebook":
                    result = self._post_to_facebook(media_path, params, is_video=False)
                elif platform == "linkedin":
                    result = self._post_to_linkedin(media_path, params)
                else:
                    result = {"success": False, "error": f"Image posting not supported for {platform}"}
                
                results[platform] = result
            except Exception as e:
                errors.append(f"{platform}: {str(e)}")
                results[platform] = {"success": False, "error": str(e)}
        
        successful = [p for p, r in results.items() if r.get("success")]
        failed = [p for p, r in results.items() if not r.get("success")]
        
        output_lines = [f"Posted image to {len(successful)}/{len(platforms)} platform(s)\n"]
        
        if successful:
            output_lines.append("✅ Successful:")
            for platform in successful:
                post_url = results[platform].get("url", "N/A")
                output_lines.append(f"  - {platform.upper()}: {post_url}")
        
        if failed or errors:
            output_lines.append("\n❌ Failed:")
            for platform in failed:
                output_lines.append(f"  - {platform.upper()}: {results[platform].get('error')}")
            for error in errors:
                output_lines.append(f"  - {error}")
        
        output = "\n".join(output_lines)
        
        if successful:
            return ToolResult.success_result(output, metadata={"results": results})
        else:
            return ToolResult.error_result(output, metadata={"results": results})
    
    async def _post_text(self, params: SocialMediaParams, cwd: Path) -> ToolResult:
        """Post text-only content to social media platform(s)."""
        if not params.caption:
            return ToolResult.error_result("caption is required for post_text action")
        
        platforms = self._get_platforms(params.platform)
        results = {}
        errors = []
        
        for platform in platforms:
            has_creds, error_msg = self._check_credentials(platform)
            if not has_creds:
                errors.append(error_msg)
                continue
            
            try:
                if platform == "twitter":
                    result = self._post_text_to_twitter(params)
                elif platform == "facebook":
                    result = self._post_text_to_facebook(params)
                elif platform == "linkedin":
                    result = self._post_text_to_linkedin(params)
                else:
                    result = {"success": False, "error": f"Text-only posting not supported for {platform}"}
                
                results[platform] = result
            except Exception as e:
                errors.append(f"{platform}: {str(e)}")
                results[platform] = {"success": False, "error": str(e)}
        
        successful = [p for p, r in results.items() if r.get("success")]
        
        output = f"Posted text to {len(successful)}/{len(platforms)} platform(s)"
        
        if successful:
            return ToolResult.success_result(output, metadata={"results": results})
        else:
            return ToolResult.error_result(output, metadata={"results": results})
    
    async def _schedule_post(self, params: SocialMediaParams, media_path: Path, cwd: Path) -> ToolResult:
        """Schedule a post for later."""
        if not params.schedule_time:
            return ToolResult.error_result("schedule_time is required for schedule_post action")
        
        # Store scheduling info
        schedule_data = {
            "platform": params.platform,
            "media_path": str(media_path) if media_path else None,
            "caption": params.caption,
            "title": params.title,
            "tags": params.tags,
            "schedule_time": params.schedule_time,
            "visibility": params.visibility
        }
        
        # Save to scheduling queue
        schedule_dir = cwd / ".social_media_queue"
        schedule_dir.mkdir(exist_ok=True)
        
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        schedule_file = schedule_dir / f"scheduled_{timestamp}.json"
        
        with open(schedule_file, "w") as f:
            json.dump(schedule_data, f, indent=2)
        
        return ToolResult.success_result(
            f"Post scheduled for {params.schedule_time}",
            metadata={
                "schedule_file": str(schedule_file),
                "schedule_time": params.schedule_time
            }
        )
    
    async def _get_analytics(self, params: SocialMediaParams, cwd: Path) -> ToolResult:
        """Get analytics for a post."""
        if not params.post_id:
            return ToolResult.error_result("post_id is required for get_analytics action")
        
        has_creds, error_msg = self._check_credentials(params.platform)
        if not has_creds:
            return ToolResult.error_result(error_msg)
        
        # This is a placeholder - actual implementation depends on platform APIs
        return ToolResult.success_result(
            f"Analytics retrieval not yet implemented for {params.platform}",
            metadata={"post_id": params.post_id, "platform": params.platform}
        )
    
    async def _delete_post(self, params: SocialMediaParams, cwd: Path) -> ToolResult:
        """Delete a post."""
        if not params.post_id:
            return ToolResult.error_result("post_id is required for delete_post action")
        
        has_creds, error_msg = self._check_credentials(params.platform)
        if not has_creds:
            return ToolResult.error_result(error_msg)
        
        # This is a placeholder - actual implementation depends on platform APIs
        return ToolResult.success_result(
            f"Post deletion not yet implemented for {params.platform}",
            metadata={"post_id": params.post_id, "platform": params.platform}
        )
    
    def _get_platforms(self, platform: str) -> list[str]:
        """Get list of platforms to post to."""
        if platform == "all":
            return ["tiktok", "instagram", "youtube", "twitter", "facebook"]
        return [platform]
    
    # Platform-specific implementations
    
    def _post_to_tiktok(self, media_path: Path, params: SocialMediaParams) -> dict:
        """Post video to TikTok using TikTok API."""
        import requests
        
        access_token = os.environ.get("TIKTOK_ACCESS_TOKEN")
        
        # TikTok requires a multi-step process:
        # 1. Initialize upload
        # 2. Upload video chunks
        # 3. Publish video
        
        try:
            # Step 1: Initialize upload
            init_url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
            
            video_size = media_path.stat().st_size
            
            init_data = {
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": video_size
                }
            }
            
            init_response = requests.post(init_url, headers=headers, json=init_data, timeout=30)
            init_response.raise_for_status()
            
            upload_url = init_response.json()["data"]["upload_url"]
            
            # Step 2: Upload video
            with open(media_path, "rb") as video_file:
                upload_response = requests.put(
                    upload_url,
                    data=video_file,
                    headers={"Content-Type": "video/mp4"},
                    timeout=300
                )
                upload_response.raise_for_status()
            
            # Step 3: Publish
            publish_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
            
            publish_data = {
                "post_info": {
                    "title": params.caption or "",
                    "privacy_level": params.visibility.upper(),
                    "disable_duet": not params.allow_duet,
                    "disable_comment": not params.allow_comments,
                    "video_cover_timestamp_ms": 1000
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_url": upload_url
                }
            }
            
            publish_response = requests.post(publish_url, headers=headers, json=publish_data, timeout=30)
            publish_response.raise_for_status()
            
            result = publish_response.json()
            
            return {
                "success": True,
                "post_id": result["data"]["publish_id"],
                "url": f"https://www.tiktok.com/@user/video/{result['data']['publish_id']}",
                "message": "Video posted to TikTok successfully"
            }
        
        except Exception as e:
            return {
                "success": False,
                "error": f"TikTok upload failed: {str(e)}"
            }
    
    def _post_to_instagram(self, media_path: Path, params: SocialMediaParams, is_video: bool) -> dict:
        """Post to Instagram using Instagram Graph API."""
        import requests
        
        access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
        ig_account_id = os.environ.get("INSTAGRAM_BUSINESS_ACCOUNT_ID")
        
        try:
            # Instagram requires uploading to a hosting service first
            # For simplicity, this example assumes the media is already hosted
            # In production, you'd upload to AWS S3 or similar first
            
            caption_text = params.caption or ""
            if params.tags:
                caption_text += " " + " ".join(f"#{tag}" for tag in params.tags)
            
            if is_video:
                # Create video container
                container_url = f"https://graph.facebook.com/v18.0/{ig_account_id}/media"
                container_params = {
                    "media_type": "REELS",
                    "video_url": f"https://your-hosting-service.com/{media_path.name}",  # Replace with actual URL
                    "caption": caption_text,
                    "access_token": access_token
                }
            else:
                # Create image container
                container_url = f"https://graph.facebook.com/v18.0/{ig_account_id}/media"
                container_params = {
                    "image_url": f"https://your-hosting-service.com/{media_path.name}",  # Replace with actual URL
                    "caption": caption_text,
                    "access_token": access_token
                }
            
            container_response = requests.post(container_url, params=container_params, timeout=30)
            container_response.raise_for_status()
            
            container_id = container_response.json()["id"]
            
            # Publish the media
            publish_url = f"https://graph.facebook.com/v18.0/{ig_account_id}/media_publish"
            publish_params = {
                "creation_id": container_id,
                "access_token": access_token
            }
            
            publish_response = requests.post(publish_url, params=publish_params, timeout=30)
            publish_response.raise_for_status()
            
            post_id = publish_response.json()["id"]
            
            return {
                "success": True,
                "post_id": post_id,
                "url": f"https://www.instagram.com/p/{post_id}/",
                "message": "Posted to Instagram successfully"
            }
        
        except Exception as e:
            return {
                "success": False,
                "error": f"Instagram upload failed: {str(e)}"
            }
    
    def _post_to_youtube(self, media_path: Path, params: SocialMediaParams) -> dict:
        """Post video to YouTube using YouTube Data API v3."""
        try:
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
            from google.oauth2.credentials import Credentials
            
            # Build credentials
            creds = Credentials(
                token=None,
                refresh_token=os.environ.get("YOUTUBE_REFRESH_TOKEN"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=os.environ.get("YOUTUBE_CLIENT_ID"),
                client_secret=os.environ.get("YOUTUBE_CLIENT_SECRET")
            )
            
            youtube = build('youtube', 'v3', credentials=creds)
            
            # Prepare video metadata
            body = {
                'snippet': {
                    'title': params.title or "Untitled Video",
                    'description': params.caption or "",
                    'tags': params.tags or [],
                    'categoryId': '22'  # People & Blogs
                },
                'status': {
                    'privacyStatus': params.visibility or 'public',
                    'selfDeclaredMadeForKids': False
                }
            }
            
            # Upload video
            media = MediaFileUpload(str(media_path), chunksize=-1, resumable=True)
            
            request = youtube.videos().insert(
                part=','.join(body.keys()),
                body=body,
                media_body=media
            )
            
            response = request.execute()
            video_id = response['id']
            
            return {
                "success": True,
                "post_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "message": "Video uploaded to YouTube successfully"
            }
        
        except Exception as e:
            return {
                "success": False,
                "error": f"YouTube upload failed: {str(e)}"
            }
    
    def _post_to_twitter(self, media_path: Path, params: SocialMediaParams, is_video: bool) -> dict:
        """Post to Twitter/X using Twitter API v2."""
        try:
            import tweepy
            
            # Authenticate
            client = tweepy.Client(
                consumer_key=os.environ.get("TWITTER_API_KEY"),
                consumer_secret=os.environ.get("TWITTER_API_SECRET"),
                access_token=os.environ.get("TWITTER_ACCESS_TOKEN"),
                access_token_secret=os.environ.get("TWITTER_ACCESS_SECRET")
            )
            
            auth = tweepy.OAuth1UserHandler(
                os.environ.get("TWITTER_API_KEY"),
                os.environ.get("TWITTER_API_SECRET"),
                os.environ.get("TWITTER_ACCESS_TOKEN"),
                os.environ.get("TWITTER_ACCESS_SECRET")
            )
            api = tweepy.API(auth)
            
            # Upload media
            media = api.media_upload(str(media_path))
            
            # Create tweet
            caption_text = params.caption or ""
            if params.tags:
                caption_text += " " + " ".join(f"#{tag}" for tag in params.tags)
            
            tweet = client.create_tweet(
                text=caption_text,
                media_ids=[media.media_id]
            )
            
            tweet_id = tweet.data['id']
            
            return {
                "success": True,
                "post_id": tweet_id,
                "url": f"https://twitter.com/user/status/{tweet_id}",
                "message": "Posted to Twitter successfully"
            }
        
        except Exception as e:
            return {
                "success": False,
                "error": f"Twitter upload failed: {str(e)}"
            }
    
    def _post_text_to_twitter(self, params: SocialMediaParams) -> dict:
        """Post text-only tweet to Twitter."""
        try:
            import tweepy
            
            client = tweepy.Client(
                consumer_key=os.environ.get("TWITTER_API_KEY"),
                consumer_secret=os.environ.get("TWITTER_API_SECRET"),
                access_token=os.environ.get("TWITTER_ACCESS_TOKEN"),
                access_token_secret=os.environ.get("TWITTER_ACCESS_SECRET")
            )
            
            caption_text = params.caption
            if params.tags:
                caption_text += " " + " ".join(f"#{tag}" for tag in params.tags)
            
            tweet = client.create_tweet(text=caption_text)
            tweet_id = tweet.data['id']
            
            return {
                "success": True,
                "post_id": tweet_id,
                "url": f"https://twitter.com/user/status/{tweet_id}"
            }
        
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    def _post_to_facebook(self, media_path: Path, params: SocialMediaParams, is_video: bool) -> dict:
        """Post to Facebook using Graph API."""
        import requests
        
        access_token = os.environ.get("FACEBOOK_ACCESS_TOKEN")
        page_id = os.environ.get("FACEBOOK_PAGE_ID")
        
        try:
            caption_text = params.caption or ""
            
            if is_video:
                url = f"https://graph.facebook.com/v18.0/{page_id}/videos"
                files = {'source': open(media_path, 'rb')}
                data = {
                    'description': caption_text,
                    'access_token': access_token
                }
                
                response = requests.post(url, files=files, data=data, timeout=300)
            else:
                url = f"https://graph.facebook.com/v18.0/{page_id}/photos"
                files = {'source': open(media_path, 'rb')}
                data = {
                    'caption': caption_text,
                    'access_token': access_token
                }
                
                response = requests.post(url, files=files, data=data, timeout=60)
            
            response.raise_for_status()
            result = response.json()
            
            return {
                "success": True,
                "post_id": result.get("id") or result.get("post_id"),
                "url": f"https://facebook.com/{result.get('id')}",
                "message": "Posted to Facebook successfully"
            }
        
        except Exception as e:
            return {
                "success": False,
                "error": f"Facebook upload failed: {str(e)}"
            }
    
    def _post_text_to_facebook(self, params: SocialMediaParams) -> dict:
        """Post text-only to Facebook."""
        import requests
        
        access_token = os.environ.get("FACEBOOK_ACCESS_TOKEN")
        page_id = os.environ.get("FACEBOOK_PAGE_ID")
        
        try:
            url = f"https://graph.facebook.com/v18.0/{page_id}/feed"
            data = {
                'message': params.caption,
                'access_token': access_token
            }
            
            response = requests.post(url, data=data, timeout=30)
            response.raise_for_status()
            result = response.json()
            
            return {
                "success": True,
                "post_id": result['id'],
                "url": f"https://facebook.com/{result['id']}"
            }
        
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    def _post_to_linkedin(self, media_path: Path, params: SocialMediaParams) -> dict:
        """Post to LinkedIn using LinkedIn API."""
        import requests
        
        access_token = os.environ.get("LINKEDIN_ACCESS_TOKEN")
        person_urn = os.environ.get("LINKEDIN_PERSON_URN")
        
        # LinkedIn posting is complex - simplified version here
        return {
            "success": False,
            "error": "LinkedIn posting requires additional implementation"
        }
    
    def _post_text_to_linkedin(self, params: SocialMediaParams) -> dict:
        """Post text to LinkedIn."""
        import requests
        
        access_token = os.environ.get("LINKEDIN_ACCESS_TOKEN")
        person_urn = os.environ.get("LINKEDIN_PERSON_URN")
        
        try:
            url = "https://api.linkedin.com/v2/ugcPosts"
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
            
            data = {
                "author": person_urn,
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {
                            "text": params.caption
                        },
                        "shareMediaCategory": "NONE"
                    }
                },
                "visibility": {
                    "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
                }
            }
            
            response = requests.post(url, headers=headers, json=data, timeout=30)
            response.raise_for_status()
            
            return {
                "success": True,
                "post_id": response.headers.get("X-RestLi-Id"),
                "url": "https://linkedin.com"
            }
        
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }