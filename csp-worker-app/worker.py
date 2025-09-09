import pika
import json
import time
import os
import logging
from elasticsearch import Elasticsearch, exceptions as es_exceptions, helpers as es_helpers
from datetime import datetime, UTC # For Python 3.11+
import signal # For graceful shutdown

# --- Configuration ---
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()

# RabbitMQ Configuration
RABBITMQ_NODES_CSV = os.environ.get('RABBITMQ_NODES_CSV', 'rabbitmq:5672')
RABBITMQ_USER = os.environ.get('RABBITMQ_USER', 'user')
RABBITMQ_PASS = os.environ.get('RABBITMQ_PASS', 'password')
QUEUE_NAME = os.environ.get('QUEUE_NAME', 'csp_reports_queue')
RABBITMQ_RETRY_DELAY = int(os.environ.get('RABBITMQ_RETRY_DELAY', 10))
RABBITMQ_CONNECTION_ATTEMPTS_PER_NODE = int(os.environ.get('RABBITMQ_CONNECTION_ATTEMPTS_PER_NODE', 3))
RABBITMQ_RETRY_DELAY_PER_NODE = int(os.environ.get('RABBITMQ_RETRY_DELAY_PER_NODE', 5))


# Elasticsearch Configuration
ELASTICSEARCH_HOSTS = os.environ.get('ELASTICSEARCH_HOSTS', 'http://elasticsearch:9200').split(',')
ELASTICSEARCH_INDEX_PREFIX = os.environ.get('ELASTICSEARCH_INDEX_PREFIX', 'csp-violations')
ES_CONNECTION_RETRIES = int(os.environ.get('ES_CONNECTION_RETRIES', 5))
ES_RETRY_DELAY = int(os.environ.get('ES_RETRY_DELAY', 10))

# Bulk Indexing Configuration
BULK_MAX_SIZE = int(os.environ.get('BULK_MAX_SIZE', 100))
BULK_MAX_SECONDS_FLUSH = int(os.environ.get('BULK_MAX_SECONDS_FLUSH', 10))
PROCESS_DATA_EVENTS_TIMEOUT = 1.0

# --- Logging Setup ---
logging.basicConfig(level=LOG_LEVEL,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(message)s')

# --- Globals ---
es_client = None
es_bulk_batch = []
shutdown_requested = False


def init_elasticsearch_client():
    """Initializes the global Elasticsearch client (es_client) with retries.
    Returns:
        bool: True if client initialized and pinged successfully, False otherwise.
    """
    global es_client
    for attempt in range(ES_CONNECTION_RETRIES):
        try:
            logging.info(f"Attempting to connect to Elasticsearch (attempt {attempt + 1}/{ES_CONNECTION_RETRIES})...")
            temp_es_client = Elasticsearch(ELASTICSEARCH_HOSTS, request_timeout=10)
            if temp_es_client.ping():
                es_client = temp_es_client
                logging.info(f"Successfully connected to Elasticsearch on attempt {attempt + 1}.")
                return True
            else:
                logging.warning(f"Elasticsearch ping failed on connection attempt {attempt + 1}.")
                
        except es_exceptions.ConnectionError as e:
            logging.warning(f"Elasticsearch connection attempt {attempt + 1} failed: {e}")
        except Exception as e:
            logging.error(f"Unexpected error initializing Elasticsearch client on attempt {attempt + 1}: {e}", exc_info=True)
        
        if attempt + 1 < ES_CONNECTION_RETRIES:
            logging.info(f"Retrying Elasticsearch connection in {ES_RETRY_DELAY} seconds...")
            time.sleep(ES_RETRY_DELAY)
    logging.error(f"Failed to connect to Elasticsearch after {ES_CONNECTION_RETRIES} attempts.")
    return False


def flush_es_batch(channel):
    """
    Flushes the accumulated batch of CSP reports to Elasticsearch using the streaming_bulk API.
    Handles ACKing/NACKing individual messages based on Elasticsearch response for each item.
    Args:
        channel: The Pika channel object for ACKing/NACKing.
    Returns:
        bool: True if flush was attempted (even if partially failed), False if ES client was unavailable.
    """
    global es_bulk_batch, es_client

    if not es_bulk_batch:
        logging.debug("Flush called but batch is empty.")
        return True 

    if not es_client or not es_client.ping():
        logging.error("Elasticsearch client not available or ping failed during flush. Attempting to NACK and requeue messages.")
        for delivery_tag, _ in es_bulk_batch:
            try:
                channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
            except Exception as e_nack_conn_down:
                logging.error(f"Failed to NACK message (delivery_tag: {delivery_tag}) during ES down flush: {e_nack_conn_down}")
        es_bulk_batch.clear()
        if not es_client and not init_elasticsearch_client():
            logging.error("Failed to re-initialize ES client during flush after it was down.")
        return False 

    logging.info(f"Flushing batch of {len(es_bulk_batch)} reports to Elasticsearch...")
    
    current_batch_to_process = list(es_bulk_batch) 
    actions_for_bulk = [action_doc for _, action_doc in current_batch_to_process]
    delivery_tags_for_bulk = [delivery_tag for delivery_tag, _ in current_batch_to_process]
    
    es_bulk_batch.clear() 

    success_acks = []
    failure_nacks_requeue_false = []
    failure_nacks_requeue_true = []

    try:
        for i, (ok, result_item) in enumerate(es_helpers.streaming_bulk(
            client=es_client, actions=actions_for_bulk, raise_on_error=False, request_timeout=30 
        )):
            delivery_tag = delivery_tags_for_bulk[i]

            if ok:
                action_type = list(result_item.keys())[0]
                doc_id = result_item[action_type].get('_id', 'N/A')
                logging.debug(f"Successfully indexed doc (delivery_tag: {delivery_tag}, ES_id: {doc_id}).")
                success_acks.append(delivery_tag)
            else:
                action_type = list(result_item.keys())[0]
                error_details = result_item[action_type].get('error', {})
                status_code = result_item[action_type].get('status', 'N/A')
                logging.error(
                    f"Failed to index document (delivery_tag: {delivery_tag}). Status: {status_code}, Error: {error_details}"
                )
                if isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 429:
                    failure_nacks_requeue_false.append(delivery_tag)
                else:
                    failure_nacks_requeue_true.append(delivery_tag)

    except es_exceptions.ConnectionError as es_conn_err:
        logging.error(f"Elasticsearch Connection Error during streaming_bulk: {es_conn_err}", exc_info=True)
        failure_nacks_requeue_true.extend(delivery_tags_for_bulk) 
        es_client = None 
        time.sleep(ES_RETRY_DELAY)
    except Exception as e:
        logging.error(f"Unexpected error during Elasticsearch streaming_bulk: {e}", exc_info=True)
        failure_nacks_requeue_false.extend(delivery_tags_for_bulk)

    for delivery_tag in success_acks:
        try:
            channel.basic_ack(delivery_tag=delivery_tag)
        except Exception as e_ack:
            logging.error(f"Error ACKing message {delivery_tag}: {e_ack}")

    for delivery_tag in failure_nacks_requeue_false:
        try:
            channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
        except Exception as e_nack_f:
            logging.error(f"Error NACKing (requeue=False) message {delivery_tag}: {e_nack_f}")
            
    for delivery_tag in failure_nacks_requeue_true:
        try:
            channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
        except Exception as e_nack_t:
            logging.error(f"Error NACKing (requeue=True) message {delivery_tag}: {e_nack_t}")
    
    return True


def process_report_message(ch, method, properties, body):
    """Processes a single CSP report message: decodes, prepares for ES, and adds to batch."""
    global es_bulk_batch
    report_identifier = f"delivery_tag: {method.delivery_tag}, approx_body_start: {body[:60]}"
    logging.info(f"Received for batching: {report_identifier}")

    try:
        raw_report_str = body.decode('utf-8')
        report_json = json.loads(raw_report_str)
        csp_data = report_json.get('csp-report', report_json)

        index_name = f"{ELASTICSEARCH_INDEX_PREFIX}-{datetime.now(UTC).strftime('%Y.%m.%d')}"
        es_doc_source = csp_data.copy()
        es_doc_source['@timestamp'] = datetime.now(UTC).isoformat()

        action = {"_index": index_name, "_source": es_doc_source}
        
        es_bulk_batch.append((method.delivery_tag, action))
        logging.debug(f"Added to batch. Current batch size: {len(es_bulk_batch)}")

        if len(es_bulk_batch) >= BULK_MAX_SIZE:
            logging.info(f"Batch size {len(es_bulk_batch)} reached MAX_SIZE ({BULK_MAX_SIZE}). Flushing...")
            if flush_es_batch(ch):
                pass # Timer reset is handled by main loop after flush
    except json.JSONDecodeError:
        logging.error(f"JSON Decode Error for {report_identifier}. Discarding message: {body!r}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    except UnicodeDecodeError:
        logging.error(f"Unicode Decode Error for {report_identifier}. Discarding message (raw bytes): {body!r}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    except Exception as e:
        logging.error(f"Critical unexpected error preparing report for batch {report_identifier}: {e}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def on_shutdown_signal(signum, frame):
    """Handles shutdown signals for graceful exit."""
    global shutdown_requested
    logging.info(f"Received shutdown signal {signum}. Initiating graceful shutdown...")
    shutdown_requested = True


def get_rabbitmq_connection_params_list():
    """
    Parses RABBITMQ_NODES_CSV and returns a list of pika.ConnectionParameters.
    """
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    params_list = []
    node_strings = RABBITMQ_NODES_CSV.split(',')
    for node_str in node_strings:
        node_str = node_str.strip()
        try:
            if ':' in node_str:
                host, port_str = node_str.split(':', 1)
                port = int(port_str)
            else: 
                host = node_str
                port = 5672 
        except ValueError:
            host = node_str 
            port = 5672 
            logging.warning(f"Could not parse port for RabbitMQ node '{node_str}'. Defaulting to host '{host}' and port {port}.")

        params_list.append(
            pika.ConnectionParameters(
                host=host,
                port=port,
                credentials=credentials,
                connection_attempts=RABBITMQ_CONNECTION_ATTEMPTS_PER_NODE,
                retry_delay=RABBITMQ_RETRY_DELAY_PER_NODE,
                heartbeat=600,
                blocked_connection_timeout=300
            )
        )
    if not params_list:
        logging.error("No RabbitMQ nodes configured. Please check RABBITMQ_NODES_CSV.")
        raise ValueError("No RabbitMQ nodes configured.")
    return params_list


def main():
    """Main worker function: connects to RabbitMQ, initializes ES client, and starts consuming messages with timed flush."""
    logging.info("Worker process starting...")
    global es_client, es_bulk_batch, shutdown_requested
    
    rmq_connection = None
    channel = None
    last_flush_time = time.time()

    signal.signal(signal.SIGINT, lambda s, f: on_shutdown_signal(s, f))
    signal.signal(signal.SIGTERM, lambda s, f: on_shutdown_signal(s, f))

    if not init_elasticsearch_client():
        logging.warning("Elasticsearch client failed to initialize on startup. Worker will attempt to initialize/re-check.")

    while not shutdown_requested:
        try:
            if not rmq_connection or rmq_connection.is_closed:
                logging.info("Attempting to connect to RabbitMQ...")
                connection_params_list = get_rabbitmq_connection_params_list()
                rmq_connection = pika.BlockingConnection(connection_params_list)
                channel = rmq_connection.channel()
                
                # Access parameters of the successfully established connection
                # The ._impl attribute often holds the underlying connection adapter (e.g., SelectConnection)
                # which in turn has a .params attribute for the ConnectionParameters object used.
                if hasattr(rmq_connection, '_impl') and rmq_connection._impl and hasattr(rmq_connection._impl, 'params'):
                    connected_params = rmq_connection._impl.params
                    logging.info(f"Successfully connected to RabbitMQ node: {connected_params.host}:{connected_params.port}")
                else:
                    # Fallback if _impl.params isn't available (should be, but good to be safe)
                    # This might happen if Pika's internal structure changes or connection object is different than expected.
                    logging.info("Successfully connected to RabbitMQ (node details unavailable via _impl.params).")

                channel.queue_declare(
                    queue=QUEUE_NAME, 
                    durable=True,
                    arguments={'x-queue-type': 'quorum'}
                )
                channel.basic_qos(prefetch_count=BULK_MAX_SIZE)
                channel.basic_consume(queue=QUEUE_NAME, on_message_callback=process_report_message)
                logging.info(f"Worker set up to consume from queue: {QUEUE_NAME}. Max batch: {BULK_MAX_SIZE}, Timed flush: {BULK_MAX_SECONDS_FLUSH}s.")
                last_flush_time = time.time()

            if rmq_connection and rmq_connection.is_open and channel and channel.is_open:
                rmq_connection.process_data_events(time_limit=PROCESS_DATA_EVENTS_TIMEOUT)
            else:
                logging.warning("RabbitMQ connection or channel is not open. Delaying before retry.")
                if not shutdown_requested:
                    time.sleep(RABBITMQ_RETRY_DELAY)
                continue

            current_time = time.time()
            if es_bulk_batch and (current_time - last_flush_time >= BULK_MAX_SECONDS_FLUSH):
                logging.info(f"Timed flush triggered ({BULK_MAX_SECONDS_FLUSH}s). Batch size: {len(es_bulk_batch)}")
                if flush_es_batch(channel):
                    last_flush_time = current_time

        except pika.exceptions.AMQPConnectionError as amqp_err:
            logging.warning(f"Failed to connect to any RabbitMQ node in list: {amqp_err}. Retrying in {RABBITMQ_RETRY_DELAY}s...")
            if rmq_connection and rmq_connection.is_open:
                rmq_connection.close()
            rmq_connection, channel = None, None
            if not shutdown_requested:
                time.sleep(RABBITMQ_RETRY_DELAY)
        except pika.exceptions.AMQPChannelError as amqp_chan_err:
            logging.error(f"RabbitMQ Channel Error in main loop: {amqp_chan_err}", exc_info=True)
            if rmq_connection and rmq_connection.is_open:
                rmq_connection.close()
            rmq_connection, channel = None, None
            if not shutdown_requested:
                time.sleep(RABBITMQ_RETRY_DELAY)
        except KeyboardInterrupt:
            logging.info("KeyboardInterrupt received in main loop. Initiating shutdown...")
            shutdown_requested = True
        except Exception as e: 
            logging.error(f"Worker main loop encountered an unexpected error: {e}. Retrying...", exc_info=True)
            if rmq_connection and rmq_connection.is_open:
                rmq_connection.close()
            rmq_connection, channel = None, None
            if not shutdown_requested:
                time.sleep(RABBITMQ_RETRY_DELAY)
    
    logging.info("Shutdown requested. Performing final cleanup...")
    if channel and channel.is_open and es_bulk_batch:
        logging.info("Flushing remaining batch before final exit...")
        flush_es_batch(channel)
    
    if rmq_connection and rmq_connection.is_open:
        logging.info("Closing RabbitMQ connection.")
        try:
            rmq_connection.close()
        except Exception as e_close:
            logging.error(f"Error closing RabbitMQ connection during final shutdown: {e_close}")
            
    logging.info("Worker shut down gracefully.")

if __name__ == '__main__':
    main()