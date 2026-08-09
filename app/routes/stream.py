import urllib.parse
import os
from flask import Blueprint, abort, request
from .manifest import MANIFEST

from app.routes import wawin_client
from app.routes.utils import respond_with
from app.mapper import get_or_create_slug_mapping
from config import Config

stream_bp = Blueprint('stream', __name__)


def process_stream_sync(stream_data, preferred_lang=None, host='localhost'):
    """Process a single stream source"""
    from app.players.zephyrflick import get_video_from_zephyrflick_player
    import asyncio
    
    player = stream_data.get('player')
    url = stream_data.get('url')
    
    if player == 'zephyrflick':
        try:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            video_url, quality, headers, subtitles = loop.run_until_complete(
                get_video_from_zephyrflick_player(url, preferred_lang, host)
            )
        except Exception as e:
            print(f"Error processing stream: {e}")
            return None
    else:
        return None
    
    if not video_url:
        return None
    
    stream_obj = {
        'title': f'[{player}][{quality}]',
        'url': video_url,
        'behaviorHints': {
            'notWebReady': True
        }
    }
    
    if headers:
        stream_obj['behaviorHints']['proxyHeaders'] = headers
    
    if subtitles:
        stream_obj['subtitles'] = [
            {'id': sub.get('id', sub['url']), 'url': sub['url'], 'lang': sub['lang']}
            for sub in subtitles
        ]
    
    return stream_obj


@stream_bp.route('/stream/<content_type>/<content_id>.json')
@stream_bp.route('/<lang>/stream/<content_type>/<content_id>.json')
def addon_stream(content_type: str, content_id: str, lang: str = None):
    content_id = urllib.parse.unquote(content_id)
    parts = content_id.split(":")

    if content_type not in MANIFEST['types']:
        abort(404)

    if len(parts) < 1 or not parts[0].startswith('tt'):
        return respond_with({'streams': []}, use_etag=False)

    imdb_id = parts[0]
    
    slug = get_or_create_slug_mapping(imdb_id)
    if not slug:
        print(f"Stream: no slug found for {imdb_id}")
        return respond_with({'streams': []}, use_etag=False)
    
    if len(parts) == 3:
        season = int(parts[1])
        episode = int(parts[2])
    else:
        season = None
        episode = None

    try:
        host = os.getenv('REDIRECT_URL') or request.headers.get('X-Forwarded-Host') or request.host
        data = wawin_client.get_episode_streams(slug, season, episode)
        streams = []
        
        for stream_data in data.get('streams', []):
            stream = process_stream_sync(stream_data, lang, host)
            if stream:
                streams.append(stream)
        
        return respond_with({'streams': streams}, use_etag=False)
    except Exception as e:
        print(f"Error getting streams: {e}")
        return respond_with({'streams': []}, use_etag=False)
