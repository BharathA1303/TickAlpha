import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
import redis.asyncio as aioredis

from app.core import cache as cache_module
from app.core.cache import get_cached_response

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Real-time Feed"])

@router.websocket("/v1/feed")
async def websocket_feed(
    websocket: WebSocket,
    token: str = Query(..., description="WebSocket authorization feed token")
):
    """
    WebSocket endpoint for real-time tick-by-tick streaming.
    Requires a valid, short-lived, single-use feed token.
    Streams tick updates published by the SimulatorManager.
    """
    # 1. Validate feed token
    token_key = f"feed_token:{token}"
    token_val = await get_cached_response(token_key)
    
    if not token_val:
        # Invalid or expired token
        logger.warning(f"WebSocket connection rejected: invalid/expired token '{token}'")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired feed token")
        return
        
    # Extract client and session details
    client_id, session_id = token_val.split(":", 1)
    
    # 2. Consume the token (single-use enforcement)
    if cache_module.redis_client:
        try:
            await cache_module.redis_client.delete(token_key)
        except Exception as e:
            logger.error(f"Failed to delete feed token in Redis: {e}")
            
    # 3. Accept connection
    await websocket.accept()
    logger.info(f"WebSocket connected. Client: {client_id}, Session: {session_id}")
    
    # 4. Subscribe to the updates feed (Redis Pub/Sub or In-memory Queue)
    from app.simulator.simulator_manager import simulator_manager
    
    # Register a *bounded* local in-memory queue for this connection. A bound is
    # critical for load protection: if a browser consumes slowly (or stalls),
    # an unbounded queue would grow until the server runs out of memory and
    # crashes. With a bound, the publisher sheds the oldest tick batch for this
    # one slow client (see SimulatorManager.publish_to_session) instead of
    # taking the whole process down.
    local_queue = asyncio.Queue(maxsize=256)
    if session_id not in simulator_manager.listeners:
        simulator_manager.listeners[session_id] = set()
    simulator_manager.listeners[session_id].add(local_queue)
    
    pubsub_task = None
    
    async def listen_to_pubsub(ps: aioredis.client.PubSub):
        try:
            async for message in ps.listen():
                if message["type"] == "message":
                    data = message["data"]
                    # Forward string straight to WebSocket
                    await websocket.send_text(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in Pub/Sub websocket forwarding loop: {e}")

    async def listen_to_local_queue():
        try:
            while True:
                msg = await local_queue.get()
                # Forward parsed JSON dict to WebSocket
                await websocket.send_json(msg)
                local_queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in local queue websocket forwarding loop: {e}")

    # Set up subscription task
    pubsub = None
    if cache_module.redis_client:
        try:
            pubsub = cache_module.redis_client.pubsub()
            await pubsub.subscribe(f"session_channel:{session_id}")
            pubsub_task = asyncio.create_task(listen_to_pubsub(pubsub))
            logger.info(f"Subscribed WebSocket to Redis Pub/Sub channel for session {session_id}")
        except Exception as e:
            logger.error(f"Failed to subscribe to Redis Pub/Sub: {e}. Falling back to local in-memory queue.")
            pubsub_task = asyncio.create_task(listen_to_local_queue())
    else:
        logger.info(f"Redis is offline. Subscribed WebSocket to local in-memory queue for session {session_id}")
        pubsub_task = asyncio.create_task(listen_to_local_queue())
        
    # 5. Read loop (handles heartbeats and client disconnects)
    try:
        while True:
            # Wait for client messages (e.g. ping heartbeats)
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type")
                
                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                elif msg_type == "heartbeat":
                    await websocket.send_json({"type": "heartbeat_ack"})
                else:
                    logger.debug(f"Received WebSocket message from client: {msg}")
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON format"})
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for client {client_id}, session {session_id}")
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
    finally:
        # 6. Cleanup tasks and unsubscribe
        if pubsub_task:
            pubsub_task.cancel()
            try:
                await pubsub_task
            except asyncio.CancelledError:
                pass
                
        # Unsubscribe local queue
        if session_id in simulator_manager.listeners:
            simulator_manager.listeners[session_id].discard(local_queue)
            if not simulator_manager.listeners[session_id]:
                del simulator_manager.listeners[session_id]
                
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(f"session_channel:{session_id}")
                await pubsub.close()
            except Exception as e:
                logger.error(f"Error cleaning up Redis Pub/Sub subscription: {e}")
