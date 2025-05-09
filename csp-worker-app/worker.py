import pika
import json
import time
import os
import logging
from elasticsearch import Elasticsearch, exceptions as es_exceptions, helpers as es_helpers # Import helpers
from datetime import datetime, UTC # Use UTC from datetime for Python 3.11+

# --- Configuration ---
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()

# RabbitMQ Configuration
RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'rabbitmq')
RABBITMQ_PORT = int(os.environ.get('RABBITMQ_PORT', 5672))
RABBITMQ_USER = os.environ.get('RABBITMQ_USER', 'user')
RABBITMQ_PASS = os.environ.get('RABBITMQ_PASS', 'password')
QUEUE_NAME = os.environ.get('QUEUE_NAME', 'csp_reports_queue')
RABBITMQ_RETRY_DELAY = int(os.environ.get('RABBITMQ_RETRY_DELAY', 10))

# Elasticsearch Configuration
ELASTICSEARCH_HOSTS = os.environ.get('ELASTICSEARCH_HOSTS', 'http://elasticsearch:9200').split(',')
ELASTICSEARCH_INDEX_PREFIX = os.environ.get('ELASTICSEARCH_INDEX_PREFIX', 'csp-violations')
ES_CONNECTION_RETRIES = int(os.environ.get('ES_CONNECTION_RETRIES', 5))
ES_RETRY_DELAY = int(os.environ.get('ES_RETRY_DELAY', 10))

# Bulk Indexing Configuration
BULK_MAX_SIZE = int(os.environ.get('BULK_MAX_SIZE', 100)) # Number of messages
# BULK_MAX_SECONDS_FLUSH = int(os.environ.get('BULK_MAX_SECONDS_FLUSH', 5)) # Timed flush is more complex with BlockingConnection

# --- Logging Setup ---
logging.basicConfig(level=LOG_LEVEL,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(message)s')

# --- Globals ---
es_client = None
# Batch will store tuples: (delivery_tag, es_action_document)
# es_action_document is a dict like {"_index": index_name, "_source": es_doc}
es_bulk_batch = []
# We need the channel object for ACKing/NACKing in flush_es_batch and shutdown
# This is tricky with callbacks. A class-based worker would handle this better.
# For now, we'll pass the channel to flush_es_batch.
# This means flush_es_batch must be called from within process_report_message or main loop context.

def init_elasticsearch_client():
    """Initializes the global Elasticsearch client (es_client) with retries.
    Returns:
        bool: True if client initialized and pinged successfully, False otherwise.
    """
    global es_client
    # ... (Keep your existing init_elasticsearch_client function as refined in message #28)
    # For brevity, I'll assume it's the same and correctly sets global es_client
    # Ensure it uses ELASTICSEARCH_HOSTS correctly
    auth = None
    last_exception = None
    for attempt in range(ES_CONNECTION_RETRIES):
        try:
            logging.info(f"Attempting to connect to Elasticsearch (attempt {attempt + 1}/{ES_CONNECTION_RETRIES})...")
            temp_es_client = Elasticsearch(ELASTICSEARCH_HOSTS, request_timeout=10) # Added request_timeout
            if temp_es_client.ping():
                es_client = temp_es_client
                logging.info(f"Successfully connected to Elasticsearch on attempt {attempt + 1}.")
                return True
            else:
                logging.warning(f"Elasticsearch ping failed on connection attempt {attempt + 1}.")
                last_exception = Exception(f"Elasticsearch ping failed on attempt {attempt + 1}")
        except es_exceptions.ConnectionError as e:
            logging.warning(f"Elasticsearch connection attempt {attempt + 1} failed: {e}")
            last_exception = e
        except Exception as e:
            logging.error(f"Unexpected error initializing Elasticsearch client on attempt {attempt + 1}: {e}", exc_info=True)
            last_exception = e
        
        if attempt + 1 < ES_CONNECTION_RETRIES:
            logging.info(f"Retrying Elasticsearch connection in {ES_RETRY_DELAY} seconds...")
            time.sleep(ES_RETRY_DELAY)

    logging.error(f"Failed to connect to Elasticsearch after {ES_CONNECTION_RETRIES} attempts.")
    if last_exception:
        # Consider if you want to raise last_exception here to stop the worker
        pass
    return False


def flush_es_batch(channel):
    """
    Flushes the accumulated batch of CSP reports to Elasticsearch using the bulk API.
    Handles ACKing/NACKing individual messages based on Elasticsearch response.
    Args:
        channel: The Pika channel object for ACKing/NACKing.
    """
    global es_bulk_batch, es_client

    if not es_bulk_batch:
        logging.debug("Flush called but batch is empty.")
        return

    if not es_client or not es_client.ping():
        logging.error("Elasticsearch client not available or ping failed during flush. Attempting to NACK and requeue messages.")
        for delivery_tag, _ in es_bulk_batch:
            try:
                channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
            except Exception as e:
                logging.error(f"Failed to NACK message (delivery_tag: {delivery_tag}) during ES down flush: {e}")
        es_bulk_batch.clear()
        if not es_client:
             if not init_elasticsearch_client():
                 logging.error("Failed to re-initialize ES client during flush after it was down.")
        return

    logging.info(f"Flushing batch of {len(es_bulk_batch)} reports to Elasticsearch...")
    
    current_batch_to_process = list(es_bulk_batch) 
    actions_for_bulk = [action_doc for _, action_doc in current_batch_to_process]
    es_bulk_batch.clear() # Clear global batch *before* sending

    try:
        # When stats_only=True, the second return value is the count of errors (an int)
        successes, num_errors = es_helpers.bulk(es_client, actions_for_bulk, stats_only=True, raise_on_error=False)
        
        # Correctly use num_errors as an integer
        logging.info(f"Bulk ES operation complete. Successes: {successes}, Failures: {num_errors}")

        if num_errors > 0:
            # If stats_only=True, we don't get detailed errors easily to NACK selectively.
            # For this simplified version, if there are any errors, we NACK all in this batch.
            # This is a known area for future improvement (use stats_only=False for per-item results).
            logging.error(f"{num_errors} errors occurred during Elasticsearch bulk operation. NACKing all messages in this batch (requeue=False).")
            for delivery_tag, _ in current_batch_to_process:
                try:
                    channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
                except Exception as e_nack:
                    logging.error(f"Error NACKing message {delivery_tag} after bulk failure: {e_nack}")
        elif successes > 0 : # Only ACK if there were successes and no errors reported by stats_only
             # (and successes should match len(current_batch_to_process) if num_errors is 0)
            logging.info(f"All {successes} documents in batch processed successfully by ES (based on stats_only). ACKing messages.")
            for delivery_tag, _ in current_batch_to_process:
                try:
                    channel.basic_ack(delivery_tag=delivery_tag)
                except Exception as e_ack:
                    logging.error(f"Error ACKing message {delivery_tag} after successful bulk: {e_ack}")
        else: # No successes and no errors (e.g. empty batch was sent, though we check for this)
            logging.info("Bulk operation reported no successes and no errors.")
            # This case might imply the batch was empty or an issue with stats_only if successes is 0 and num_errors is 0
            # For safety, if we got here with items in current_batch_to_process, it's an odd state.
            # However, if current_batch_to_process was not empty, and successes and num_errors are both 0,
            # something is unusual. It might be safer to NACK them to avoid losing them.
            if current_batch_to_process:
                logging.warning("Bulk stats reported 0 successes and 0 errors for a non-empty batch. NACKing with requeue for safety.")
                for delivery_tag, _ in current_batch_to_process:
                    try:
                        channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
                    except Exception as e_nack:
                        logging.error(f"Error NACKing message {delivery_tag} in ambiguous bulk stats case: {e_nack}")


    except es_exceptions.ConnectionError as es_conn_err:
        logging.error(f"Elasticsearch Connection Error during bulk flush: {es_conn_err}", exc_info=True)
        for delivery_tag, _ in current_batch_to_process: 
            try:
                channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
            except Exception as e_nack:
                 logging.error(f"Error NACKing (requeue=True) message {delivery_tag} after bulk connection failure: {e_nack}")
        es_client = None 
        time.sleep(ES_RETRY_DELAY)
    except Exception as e: # Catch other unexpected errors during the bulk call itself
        logging.error(f"Unexpected error during Elasticsearch bulk flush logic: {e}", exc_info=True)
        for delivery_tag, _ in current_batch_to_process:
            try: 
                channel.basic_nack(delivery_tag=delivery_tag, requeue=False) 
            except Exception as e_nack:
                logging.error(f"Error NACKing (requeue=False) message {delivery_tag} after bulk general error: {e_nack}")


def process_report_message(ch, method, properties, body):
    """Processes a single CSP report message: decodes, prepares for ES, and adds to batch."""
    global es_bulk_batch # Ensure we're using the global batch
    report_identifier = f"delivery_tag: {method.delivery_tag}, approx_body_start: {body[:60]}"
    logging.info(f"Received for batching: {report_identifier}")

    try:
        raw_report_str = body.decode('utf-8')
        report_json = json.loads(raw_report_str)
        csp_data = report_json.get('csp-report', report_json)

        index_name = f"{ELASTICSEARCH_INDEX_PREFIX}-{datetime.now(UTC).strftime('%Y.%m.%d')}"
        es_doc_source = csp_data.copy()
        es_doc_source['@timestamp'] = datetime.now(UTC).isoformat()

        # Action for Elasticsearch bulk API
        action = {
            "_index": index_name,
            "_source": es_doc_source
            # You could add an "_id": here if you have a unique ID for each report to make it idempotent
        }
        
        es_bulk_batch.append((method.delivery_tag, action)) # Store delivery_tag and the action
        logging.debug(f"Added to batch. Current batch size: {len(es_bulk_batch)}")

        if len(es_bulk_batch) >= BULK_MAX_SIZE:
            logging.info(f"Batch size {len(es_bulk_batch)} reached. Flushing...")
            flush_es_batch(ch) # Pass the channel

    except json.JSONDecodeError as json_err:
        logging.error(f"JSON Decode Error for {report_identifier}. Discarding message: {body!r}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    except UnicodeDecodeError as ude_err:
        logging.error(f"Unicode Decode Error for {report_identifier}. Discarding message (raw bytes): {body!r}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    except Exception as e:
        logging.error(f"Critical unexpected error preparing report for batch {report_identifier}: {e}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def main():
    """Main worker function: connects to RabbitMQ, initializes ES client, and starts consuming messages."""
    logging.info("Worker process starting...")
    global es_client # Ensure main function knows about the global
    global channel # Make channel accessible for shutdown hook if not passed

    if not init_elasticsearch_client():
        logging.warning("Elasticsearch client failed to initialize on startup. Worker will attempt to initialize/re-check on first message or flush.")

    rmq_connection = None # Keep connection reference for graceful shutdown
    channel = None # Keep channel reference

    def on_shutdown_signal(signum, frame):
        logging.info(f"Received shutdown signal {signum}. Flushing batch and closing connections...")
        if channel and channel.is_open and es_bulk_batch: # Check if channel is available and batch has items
            logging.info("Flushing remaining batch before shutdown...")
            flush_es_batch(channel)
        if rmq_connection and rmq_connection.is_open:
            logging.info("Closing RabbitMQ connection.")
            rmq_connection.close()
        logging.info("Worker shut down gracefully.")
        exit(0)

    import signal
    signal.signal(signal.SIGINT, on_shutdown_signal)
    signal.signal(signal.SIGTERM, on_shutdown_signal)

    while True:
        try:
            credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
            connection_params = pika.ConnectionParameters(
                host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials,
                heartbeat=600, blocked_connection_timeout=300
            )
            rmq_connection = pika.BlockingConnection(connection_params) # Assign to rmq_connection
            # Removed 'with' statement here to keep rmq_connection and channel alive for shutdown hook
            # This means we need explicit close on controlled exit.

            channel = rmq_connection.channel() # Assign to global-like channel
            logging.info("Successfully connected to RabbitMQ.")
            
            channel.queue_declare(queue=QUEUE_NAME, durable=True)
            channel.basic_qos(prefetch_count=BULK_MAX_SIZE) # Prefetch up to batch size
            channel.basic_consume(queue=QUEUE_NAME, on_message_callback=process_report_message)
            
            logging.info(f"Worker consuming messages on queue: {QUEUE_NAME}. Max batch size: {BULK_MAX_SIZE}. Waiting for messages...")
            channel.start_consuming() # This will block until connection closes or an exception not caught by Pika

        except pika.exceptions.AMQPConnectionError as amqp_err:
            logging.warning(f"RabbitMQ Connection Error: {amqp_err}. Retrying in {RABBITMQ_RETRY_DELAY}s...")
        except pika.exceptions.AMQPChannelError as amqp_chan_err:
            logging.error(f"RabbitMQ Channel Error: {amqp_chan_err}", exc_info=True)
        except KeyboardInterrupt: # Handle Ctrl+C for graceful shutdown
            logging.info("KeyboardInterrupt received. Shutting down...")
            # The signal handler should take care of flushing and closing
            break # Exit the while True loop
        except Exception as e:
            logging.error(f"Worker main loop encountered an unexpected error: {e}. Retrying connection...", exc_info=True)
        finally: # This finally is for the main RMQ connection loop
            if channel and channel.is_open: # If loop broke due to error other than KeyboardInterrupt, try to flush
                if es_bulk_batch:
                    logging.info("Main loop exiting, attempting final flush...")
                    try:
                        flush_es_batch(channel)
                    except Exception as fe:
                        logging.error(f"Error during final flush: {fe}")
            if rmq_connection and rmq_connection.is_open:
                logging.info("Closing RabbitMQ connection in main finally block.")
                rmq_connection.close()
            
        logging.info(f"Attempting to reconnect to RabbitMQ after a delay of {RABBITMQ_RETRY_DELAY}s...")
        time.sleep(RABBITMQ_RETRY_DELAY)

if __name__ == '__main__':
    main()