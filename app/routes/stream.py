import urllib.parse
from flask import Blueprint, abort, jsonify
from .manifest import MANIFEST

from app.routes import wawin_client
from app.routes.utils import respond_with
from app.database import db
from app.mapper import get_or_create_slug_mapping, get_tmdb_details_from_imdb
from app.routes import wawin_client as wawin
from config import Config

stream_bp = Blueprint('stream', __name__)


def process_stream_sync(stream_data, preferred_lang=None):
    """Process a single stream source"""
    from app.players.zephyrflick import get_video_from_zephyrflick_player
    import asyncio
    
    player = stream_data.get('player')
    url = stream_data.get('url')
    
    if player == 'zephyrflick':
        try:
            # Create new event loop (no nest_asyncio needed in Python 3.10+)
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            video_url, quality, headers, subtitles = loop.run_until_complete(
                get_video_from_zephyrflick_player(url, preferred_lang)
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
            'notWebReady': True  # HLS without CORS - not playable in Stremio Web
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
    """
    Provide stream URLs
    :param content_type: The type of content
    :param content_id: The id of the content (tt13706018:3:2 for series or tt13706018 for movies)
    :param lang: Optional preferred audio language (e.g. 'hin', 'eng', 'jpn')
    :return: JSON response
    """
    content_id = urllib.parse.unquote(content_id)
    parts = content_id.split(":")

    if content_type not in MANIFEST['types']:
        abort(404)

    if len(parts) < 1 or not parts[0].startswith('tt'):
        return respond_with({'streams': []}, use_etag=False)

    imdb_id = parts[0]
    
    # Find or create slug mapping from IMDB ID
    slug = get_or_create_slug_mapping(imdb_id)
    if not slug:
        return respond_with({'streams': []}, use_etag=False)
    
    # For series: tt13706018:3:2, for movies: tt13706018
    if len(parts) == 3:
        season = int(parts[1])
        episode = int(parts[2])
    else:
        # Movies don't have season/episode
        season = None
        episode = None

    try:
        data = wawin_client.get_episode_streams(slug, season, episode)
        streams = []
        
        for stream_data in data.get('streams', []):
            stream = process_stream_sync(stream_data, lang)
            if stream:
                streams.append(stream)
        
        return respond_with({'streams': streams}, use_etag=False)
    except Exception as e:
        print(f"Error getting streams: {e}")
        return respond_with({'streams': []}, use_etag=False)


@stream_bp.route('/debug/imdb2slug/<imdb_id>')
def debug_imdb2slug(imdb_id: str):
    """Debug endpoint to trace IMDB → slug mapping"""
    result = {
        'imdb_id': imdb_id,
        'db_type': Config.DB_TYPE,
        'tmdb_key_set': bool(Config.TMDB_API_KEY),
        'steps': {}
    }
    
    # Step 1: Check DB mapping
    slug = db.get_slug_by_imdb(imdb_id)
    result['steps']['db_lookup'] = {'found': slug is not None, 'slug': slug}
    if slug:
        result['final_slug'] = slug
        return jsonify(result)
    
    # Step 2: TMDB details from IMDB
    try:
        tmdb_details = get_tmdb_details_from_imdb(imdb_id)
        result['steps']['tmdb_find'] = {'success': tmdb_details is not None, 'data': tmdb_details}
    except Exception as e:
        result['steps']['tmdb_find'] = {'success': False, 'error': str(e)}
        return jsonify(result)
    
    if not tmdb_details:
        return jsonify(result)
    
    title = tmdb_details.get('title') or tmdb_details.get('name')
    poster_path = tmdb_details.get('poster_path')
    tmdb_id = str(tmdb_details['id'])
    media_type = tmdb_details.get('media_type')
    
    result['steps']['tmdb_details'] = {
        'title': title,
        'poster_path': poster_path,
        'tmdb_id': tmdb_id,
        'media_type': media_type
    }
    
    if not title:
        return jsonify(result)
    
    # Step 3: WAWIN search
    try:
        search_results = wawin.search_anime(title)
        result['steps']['wawin_search'] = {
            'success': True,
            'count': len(search_results),
            'results': [{'slug': r.get('slug'), 'title': r.get('title'), 'poster': r.get('poster', '')[:80]} for r in search_results[:3]]
        }
    except Exception as e:
        result['steps']['wawin_search'] = {'success': False, 'error': str(e)}
        return jsonify(result)
    
    if not search_results:
        return jsonify(result)
    
    # Step 4: Poster match
    try:
        from app.mapper import match_by_poster
        matched = match_by_poster(poster_path, search_results, tmdb_id, media_type)
        result['steps']['poster_match'] = {'success': matched is not None, 'matched_slug': matched.get('slug') if matched else None}
    except Exception as e:
        result['steps']['poster_match'] = {'success': False, 'error': str(e)}
        return jsonify(result)
    
    if matched:
        slug = matched.get('slug')
        result['final_slug'] = slug
    else:
        result['final_slug'] = None
    
    return jsonify(result)


@stream_bp.route('/debug/stream/<content_type>/<content_id>')
def debug_stream(content_type: str, content_id: str):
    """Debug endpoint to trace full stream flow"""
    result = {
        'content_type': content_type,
        'content_id': content_id,
        'steps': {}
    }
    
    content_id = urllib.parse.unquote(content_id)
    parts = content_id.split(":")
    
    if len(parts) < 1 or not parts[0].startswith('tt'):
        result['error'] = 'Invalid content_id format'
        return jsonify(result)
    
    imdb_id = parts[0]
    if len(parts) == 3:
        season = int(parts[1])
        episode = int(parts[2])
    else:
        season = None
        episode = None
    
    result['parsed'] = {'imdb_id': imdb_id, 'season': season, 'episode': episode}
    
    # Step 1: Get slug
    slug = get_or_create_slug_mapping(imdb_id)
    result['steps']['get_slug'] = {'slug': slug}
    if not slug:
        result['error'] = 'No slug found'
        return jsonify(result)
    
    # Step 2: Get episode streams from WAWIN
    try:
        data = wawin.get_episode_streams(slug, season, episode)
        result['steps']['get_episode_streams'] = {
            'success': True,
            'streams_found': len(data.get('streams', [])),
            'streams': data.get('streams', [])
        }
    except Exception as e:
        result['steps']['get_episode_streams'] = {'success': False, 'error': str(e)}
        return jsonify(result)
    
    if not data.get('streams'):
        result['error'] = 'No streams from WAWIN'
        return jsonify(result)
    
    # Step 3: Process each stream
    processed = []
    for stream_data in data.get('streams', []):
        try:
            import asyncio
            import nest_asyncio
            from app.players.zephyrflick import get_video_from_zephyrflick_player
            
            nest_asyncio.apply()
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            video_url, quality, headers, subtitles = loop.run_until_complete(
                get_video_from_zephyrflick_player(stream_data['url'], None)
            )
            
            processed.append({
                'player_url': stream_data['url'],
                'video_url': video_url,
                'quality': quality,
                'subtitles': len(subtitles) if subtitles else 0,
                'success': video_url is not None
            })
        except Exception as e:
            processed.append({'player_url': stream_data['url'], 'error': str(e), 'success': False})
    
    result['steps']['process_streams'] = processed
    return jsonify(result)
