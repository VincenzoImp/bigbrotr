import time
import uuid
import json
import asyncio
import logging
from event import Event
from bigbrotr import Bigbrotr
from aiohttp_socks import ProxyConnector
from aiohttp import ClientSession, WSMsgType, TCPConnector


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


def get_start_time(default_start_time, bigbrotr, relay, retries=5, delay=30):
    def get_max_seen_at():
        query = """
            SELECT MAX(seen_at)
            FROM events_relays
            WHERE relay_url = %s
        """
        bigbrotr.execute(query, (relay.url,))
        return bigbrotr.fetchone()[0]

    def get_event_id(max_seen_at):
        query = """
            SELECT event_id
            FROM events_relays
            WHERE relay_url = %s AND seen_at = %s
            LIMIT 1
        """
        bigbrotr.execute(query, (relay.url, max_seen_at))
        return bigbrotr.fetchone()[0]

    def get_created_at(event_id):
        query = """
            SELECT created_at
            FROM events
            WHERE id = %s
        """
        bigbrotr.execute(query, (event_id,))
        return bigbrotr.fetchone()[0]
    max_seen_at_todo = True
    max_seen_at = None
    event_id_todo = True
    event_id = None
    created_at_todo = True
    created_at = None
    bigbrotr.connect()
    for attempt in range(retries):
        try:
            if max_seen_at_todo:
                max_seen_at = get_max_seen_at()
                max_seen_at_todo = False
            if max_seen_at is not None:
                if event_id_todo:
                    event_id = get_event_id(max_seen_at)
                    event_id_todo = False
                if event_id is not None:
                    if created_at_todo:
                        created_at = get_created_at(event_id)
                        created_at_todo = False
                    if created_at is not None:
                        return created_at + 1
            return default_start_time
        except Exception as e:
            logging.warning(
                f"⚠️ Attempt {attempt + 1}/{retries} failed while getting start time for {relay.url}: {e}")
            time.sleep(delay)
    bigbrotr.close()
    raise RuntimeError(
        f"❌ Failed to get start time for {relay.url} after {retries} attempts. Last error: {e}")


async def get_max_limit(filter, session, relay_url, timeout, start_time, end_time):
    n_events = [0, 0]
    min_created_at = None
    since = start_time
    until = end_time
    for attempt in range(2):
        subscription_id = uuid.uuid4().hex
        request = json.dumps([
            "REQ",
            subscription_id,
            {**filter, "since": since, "until": until}
        ])
        async with session.ws_connect(relay_url, timeout=timeout) as ws:
            await ws.send_str(request)
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=timeout*10)
                    if msg.type == WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data[0] == "NOTICE":
                            continue
                        elif data[0] == "EVENT" and data[1] == subscription_id:
                            if attempt == 0:
                                if isinstance(data[2], dict) and "created_at" in data[2]:
                                    if min_created_at is None or data[2]["created_at"] < min_created_at:
                                        min_created_at = data[2]["created_at"]
                            n_events[attempt] += 1
                        elif data[0] == "EOSE" and data[1] == subscription_id:
                            await ws.send_str(json.dumps(["CLOSE", subscription_id]))
                            await asyncio.sleep(1)
                            break
                        elif data[0] == "CLOSED" and data[1] == subscription_id:
                            break
                    else:
                        break
                except Exception:
                    break
            if attempt == 0:
                if min_created_at is not None:
                    until = max(0, min(min_created_at, until) - 1)
                else:
                    return None
    if n_events[1] > 0:
        return n_events[0]
    return None


def create_event(event_data):
    try:
        event = Event.from_dict(event_data)
    except ValueError as e:
        tags = []
        for tag in event_data['tags']:
            tag = [
                t.replace(r'\n', '\n').replace(r'\"', '\"').replace(r'\\', '\\').replace(
                    r'\r', '\r').replace(r'\t', '\t').replace(r'\b', '\b').replace(r'\f', '\f')
                for t in tag
            ]
            tags.append(tag)
        event_data['tags'] = tags
        event_data['content'] = event_data['content'].replace(r'\n', '\n').replace(r'\"', '\"').replace(
            r'\\', '\\').replace(r'\r', '\r').replace(r'\t', '\t').replace(r'\b', '\b').replace(r'\f', '\f')
        event = Event.from_dict(event_data)
    return event


def insert_batch(bigbrotr, batch, relay, seen_at):
    event_batch = []
    for event_data in batch:
        try:
            event = create_event(event_data)
        except Exception as e:
            logging.warning(
                f"⚠️ Invalid event found in {relay.url}. Error: {e}")
            continue
        event_batch.append(event)
    bigbrotr.insert_event_batch(event_batch, relay, seen_at)
    return len(event_batch)


async def process_relay(config, relay, bigbrotr, start_time, end_time):
    socks5_proxy_url = f"socks5://{config['torhost']}:{config['torport']}"
    bigbrotr.connect()
    skip = False
    try:
        for schema in ['wss://', 'ws://']:
            if skip:
                break
            try:
                if relay.network == 'tor':
                    connector = ProxyConnector.from_url(
                        socks5_proxy_url, force_close=True)
                else:
                    connector = TCPConnector(force_close=True)
                async with ClientSession(connector=connector) as session:
                    relay_id = relay.url.removeprefix('wss://')
                    timeout = config["timeout"]
                    n_events_inserted = 0
                    n_requests_done = 0
                    n_writes = 0
                    stack = [end_time]
                    stack_max_size = 1000
                    max_limit = await get_max_limit(config["filter"], session, schema + relay_id, timeout, start_time, end_time)
                    max_limit = max_limit if max_limit is not None else 500
                    max_limit = min(max_limit, 2000)
                    max_limit = max(
                        1, max_limit - 50 if max_limit >= 100 else max_limit - 5)
                    async with session.ws_connect(schema + relay_id, timeout=timeout) as ws:
                        skip = True
                        while start_time <= end_time:
                            since = start_time
                            until = stack.pop()
                            while since <= until:
                                if n_requests_done % 25 == 0:
                                    logging.info(
                                        f"🔄 [Processing {relay.url}] [from {since}] [to {until}] [max limit {max_limit}] [requests done {n_requests_done} ({n_writes} with events)] [requests todo {len(stack)+1}] [events inserted {n_events_inserted}]")
                                subscription_id = uuid.uuid4().hex
                                batch = []
                                request = json.dumps([
                                    "REQ",
                                    subscription_id,
                                    {**config["filter"],
                                        "since": since, "until": until}
                                ])
                                await ws.send_str(request)
                                while True:
                                    msg = await asyncio.wait_for(ws.receive(), timeout=timeout*10)
                                    if msg.type == WSMsgType.TEXT:
                                        data = json.loads(msg.data)
                                        if data[0] == "NOTICE":
                                            logging.info(
                                                f"📢 NOTICE received from {relay.url}: {data}")
                                            continue
                                        elif data[0] == "EVENT" and data[1] == subscription_id:
                                            if isinstance(data[2], dict):
                                                created_at = data[2].get(
                                                    'created_at')
                                                if isinstance(created_at, int) and since <= created_at <= until:
                                                    batch.append(data[2])
                                        elif data[0] == "EOSE" and data[1] == subscription_id:
                                            await ws.send_str(json.dumps(["CLOSE", subscription_id]))
                                            await asyncio.sleep(1)
                                            break
                                        elif data[0] == "CLOSED" and data[1] == subscription_id:
                                            break
                                        if len(batch) >= max_limit and since != until:
                                            stack.append(until)
                                            until = since + \
                                                (until - since) // 2
                                            if len(stack) > stack_max_size:
                                                stack.pop(0)
                                                end_time = stack[0]
                                            await ws.send_str(json.dumps(["CLOSE", subscription_id]))
                                            await asyncio.sleep(1)
                                            break
                                    elif msg.type == WSMsgType.ERROR:
                                        raise RuntimeError(
                                            f"WebSocket error from {relay.url}: {msg.data}")
                                    elif msg.type == WSMsgType.CLOSED:
                                        raise RuntimeError(
                                            f"WebSocket closed by {relay.url}")
                                    else:
                                        raise RuntimeError(
                                            f"Unexpected message type: {msg.type} from {relay.url}")
                                if len(batch) < max_limit or since == until:
                                    n_events_inserted += insert_batch(
                                        bigbrotr, batch, relay, int(time.time()))
                                    start_time = until + 1
                                    since = until + 1
                                    n_writes += 1
                                n_requests_done += 1
                bigbrotr.close()
                logging.info(
                    f"✅ Finished processing {relay.url}. Total events inserted: {n_events_inserted}")
                return
            except Exception as e:
                logging.exception(
                    f"⚠️ Unexpected error while processing {relay.url}: {e}")
    except Exception as e:
        bigbrotr.close()
