import pika
import json
import time
import os
import logging
from elasticsearch import Elasticsearch, exceptions as es_exceptions # Import Elasticsearch
from datetime import datetime, timezone, UTC

# --- Configuration ---
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()

# RabbitMQ Configuration
RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'rabbitmq')
RABBITMQ_PORT = int(os.environ.get('RABBITMQ_PORT', 5672))
RABBITMQ_USER = os.environ.get('RABBITMQ_USER', 'user')
RABBITMQ_PASS = os.environ.get('RABBITMQ_PASS', 'password')
QUEUE_NAME = os.environ.get('QUEUE_NAME', 'csp_reports_queue')
RABBITMQ_RETRY_DELAY = int(os.environ.get('RABBITMQ_RETRY_DELAY', 10)) # seconds

# Elasticsearch Configuration
ELASTICSEARCH_HOSTS = os.environ.get('ELASTICSEARCH_HOSTS', 'http://elasticsearch:9200').split(',') # Comma-separated for multiple nodes
# Example for basic auth:
# ELASTICSEARCH_USER = os.environ.get('ELASTICSEARCH_USER')
# ELASTICSEARCH_PASS = os.environ.get('ELASTICSEARCH_PASS')
ELASTICSEARCH_INDEX_PREFIX = os.environ.get('ELASTICSEARCH_INDEX_PREFIX', 'csp-violations')
ES_CONNECTION_RETRIES = int(os.environ.get('ES_CONNECTION_RETRIES', 3))
ES_RETRY_DELAY = int(os.environ.get('ES_RETRY_DELAY', 5)) # seconds


# --- Logging Setup ---
logging.basicConfig(level=LOG_LEVEL,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(message)s')

# --- Elasticsearch Client ---
es_client = None

def init_elasticsearch_client():
    """Initializes the Elasticsearch client with retries."""
    global es_client
    auth = None
    # if ELASTICSEARCH_USER and ELASTICSEARCH_PASS: # Basic Auth Example
    #     auth = (ELASTICSEARCH_USER, ELASTICSEARCH_PASS)

    # Simplified connection using ELASTICSEARCH_HOSTS which can include scheme, host, port
    # Example: http://localhost:9200,https://user:pass@otherhost:443
    # For more complex auth (API keys, Cloud ID), refer to elasticsearch-py docs
    
    last_exception = None
    for attempt in range(ES_CONNECTION_RETRIES):
        try:
            logging.info(f"Attempting to connect to Elasticsearch (attempt {attempt + 1}/{ES_CONNECTION_RETRIES})...")
            # The client now takes a list of hosts
            temp_es_client = Elasticsearch(
                ELASTICSEARCH_HOSTS
                # http_auth=auth, # if using basic auth
                # scheme="https", # If all hosts use https and not specified in ELASTICSEARCH_HOSTS
                # verify_certs=True, # Set to False for self-signed certs in dev (not recommended for prod)
                # ca_certs=os.environ.get('ES_CA_CERTS_PATH') # If using custom CA
            )
            if temp_es_client.ping():
                es_client = temp_es_client
                logging.info("Successfully connected to Elasticsearch.")
                return True
            else:
                logging.warning("Elasticsearch ping failed on attempt {attempt + 1}.")
                last_exception = Exception("Elasticsearch ping failed") # More specific error could be raised by ping
        except es_exceptions.ConnectionError as e:
            logging.warning(f"Elasticsearch connection attempt {attempt + 1} failed: {e}")
            last_exception = e
        except Exception as e: # Catch other potential init errors
            logging.error(f"Unexpected error initializing Elasticsearch client on attempt {attempt + 1}: {e}", exc_info=True)
            last_exception = e
        
        if attempt + 1 < ES_CONNECTION_RETRIES:
            logging.info(f"Retrying Elasticsearch connection in {ES_RETRY_DELAY} seconds...")
            time.sleep(ES_RETRY_DELAY)

    logging.error(f"Failed to connect to Elasticsearch after {ES_CONNECTION_RETRIES} attempts.")
    if last_exception:
        # You might want to raise it or handle it to prevent worker from starting if ES is critical
        pass
    return False


def process_report_message(ch, method, properties, body):
    """Processes a single CSP report message: decodes, and sends to Elasticsearch."""
    report_identifier = f"delivery_tag: {method.delivery_tag}, approx_body_start: {body[:60]}"
    logging.info(f"Processing report: {report_identifier}")

    global es_client # Ensure es_client is accessible
    if not es_client or not es_client.ping(): # Check connection, try to re-init if down
        logging.warning(f"Elasticsearch client not connected for report {report_identifier}. Attempting to re-initialize...")
        if not init_elasticsearch_client() or not es_client: # init_elasticsearch_client should set global es_client
            logging.error(f"Failed to re-initialize Elasticsearch client for report {report_identifier}. NACKing and requeueing.")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            time.sleep(ES_RETRY_DELAY if 'ES_RETRY_DELAY' in globals() else 5) # Use configured delay
            return

    try:
        raw_report_str = body.decode('utf-8')
        report_json = json.loads(raw_report_str)
        csp_data = report_json.get('csp-report', report_json)

        index_name = f"{ELASTICSEARCH_INDEX_PREFIX}-{datetime.now(UTC).strftime('%Y.%m.%d')}"
        es_doc = csp_data.copy()
        es_doc['@timestamp'] = datetime.now(UTC).isoformat()

        try:
            resp = es_client.index(index=index_name, document=es_doc)
            logging.info(f"Report indexed to Elasticsearch: index={index_name}, id={resp.get('_id')}, for {report_identifier}")
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except es_exceptions.ConnectionError as es_conn_err:
            logging.error(f"Elasticsearch Connection Error during indexing for report {report_identifier}: {es_conn_err}", exc_info=True)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True) # Requeue on connection errors
            es_client = None # Force re-init on next message or attempt
            time.sleep(ES_RETRY_DELAY if 'ES_RETRY_DELAY' in globals() else 5)

        except es_exceptions.TransportError as es_transport_err:
            # Log more details from TransportError
            error_message = str(es_transport_err)
            status_code = es_transport_err.status_code if hasattr(es_transport_err, 'status_code') else "N/A"
            error_info = es_transport_err.info if hasattr(es_transport_err, 'info') else {} # Detailed error info from ES

            logging.error(
                f"Elasticsearch Transport Error (HTTP Status: {status_code}) for report {report_identifier}: {error_message}. Info: {error_info}",
                exc_info=True
            )
            # For 4xx errors (e.g., bad request, mapping errors), generally don't requeue.
            # For 5xx errors (server-side issues in ES), requeueing might be an option after a delay.
            if isinstance(status_code, int) and 400 <= status_code < 500:
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False) # Bad request, mapping issue, etc. -> DLQ
            else:
                # For 5xx errors or unknown status, a limited requeue might be okay,
                # but for simplicity and safety against loops, False is often better without a retry counter.
                # Let's stick to no requeue for TransportErrors for now unless it's clearly a server overload (503).
                # If it's a 503 (Service Unavailable), we might want to requeue.
                if status_code == 503:
                    logging.warning(f"Elasticsearch service unavailable (503) for report {report_identifier}. Requeueing.")
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                    es_client = None # Force re-init
                    time.sleep(ES_RETRY_DELAY if 'ES_RETRY_DELAY' in globals() else 10) # Longer delay for 503
                else:
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False) # Other transport errors -> DLQ

        except Exception as es_gen_err: # Other generic errors from the ES client call
            logging.error(f"Unexpected error during Elasticsearch indexing for report {report_identifier}: {es_gen_err}", exc_info=True)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False) # To DLQ

    except json.JSONDecodeError as json_err:
        logging.error(f"JSON Decode Error for report {report_identifier}. Discarding message: {body!r}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    except UnicodeDecodeError as ude_err:
        logging.error(f"Unicode Decode Error for report {report_identifier}. Discarding message (raw bytes): {body!r}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    except Exception as e: # Catch-all for any other phase of processing the message
        logging.error(f"Critical unexpected error processing report {report_identifier}: {e}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False) # Safer not to requeue unknown errors
def main():
    """Main worker function: connects to RabbitMQ, initializes ES client, and starts consuming messages."""
    logging.info("Worker process starting...")

    # Initialize Elasticsearch client (with retries)
    if not init_elasticsearch_client():
        # Decide if worker should exit or keep retrying in main loop if ES is absolutely critical at startup
        logging.warning("Elasticsearch client failed to initialize on startup. Worker will attempt to initialize on first message.")
        # If ES is critical, you might exit:
        # logging.critical("Elasticsearch is critical and failed to initialize. Worker exiting.")
        # return

    connection = None
    while True:
        try:
            credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
            connection_params = pika.ConnectionParameters(
                host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials,
                heartbeat=600, blocked_connection_timeout=300
            )
            with pika.BlockingConnection(connection_params) as connection:
                logging.info("Successfully connected to RabbitMQ.")
                with connection.channel() as channel:
                    channel.queue_declare(queue=QUEUE_NAME, durable=True)
                    channel.basic_qos(prefetch_count=1)
                    channel.basic_consume(queue=QUEUE_NAME, on_message_callback=process_report_message)
                    logging.info(f"Worker consuming messages on queue: {QUEUE_NAME}. Waiting for messages...")
                    channel.start_consuming()
        except pika.exceptions.AMQPConnectionError as amqp_err:
            logging.warning(f"RabbitMQ Connection Error: {amqp_err}. Retrying in {RABBITMQ_RETRY_DELAY}s...")
        except pika.exceptions.AMQPChannelError as amqp_chan_err: # More specific Pika errors
            logging.error(f"RabbitMQ Channel Error (will attempt to reconnect): {amqp_chan_err}", exc_info=True)
        except Exception as e:
            logging.error(f"Worker main loop encountered an unexpected error: {e}. Retrying connection...", exc_info=True)
        
        logging.info(f"Attempting to reconnect to RabbitMQ after a delay of {RABBITMQ_RETRY_DELAY}s...")
        time.sleep(RABBITMQ_RETRY_DELAY)

if __name__ == '__main__':
    main()