import pika
import json
import psycopg2
import time
import os
import logging

# Configure logging - GOOD
logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO').upper(), # Suggestion: Make log level configurable
                    format='%(asctime)s - %(levelname)s - %(module)s - %(message)s') # Suggestion: Add module name

# RabbitMQ Configuration - GOOD
RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'rabbitmq')
RABBITMQ_PORT = int(os.environ.get('RABBITMQ_PORT', 5672)) # Consider error handling for int conversion
RABBITMQ_USER = os.environ.get('RABBITMQ_USER', 'user')
RABBITMQ_PASS = os.environ.get('RABBITMQ_PASS', 'password')
QUEUE_NAME = os.environ.get('QUEUE_NAME', 'csp_reports_queue')

# PostgreSQL Configuration - GOOD
DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_PORT = int(os.environ.get('DB_PORT', 5432)) # Consider error handling for int conversion
DB_NAME = os.environ.get('DB_NAME', 'csp_reports_db')
DB_USER = os.environ.get('DB_USER', 'csp_user')
DB_PASS = os.environ.get('DB_PASS', 'csp_pass')

# Suggestion: Constants for retry logic
DB_CONNECTION_RETRIES = int(os.environ.get('DB_CONNECTION_RETRIES', 3))
DB_RETRY_DELAY = int(os.environ.get('DB_RETRY_DELAY', 5)) # seconds

def get_db_connection():
    # Suggestion: Add docstring
    """Establishes and returns a connection to PostgreSQL, with retries."""
    last_exception = None
    for attempt in range(DB_CONNECTION_RETRIES):
        try:
            logging.debug(f"Attempting DB connection (attempt {attempt + 1}/{DB_CONNECTION_RETRIES}) to {DB_HOST}:{DB_PORT}/{DB_NAME}")
            conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
            logging.info(f"Successfully connected to PostgreSQL database: {DB_NAME}")
            return conn
        except psycopg2.OperationalError as e: # Catch specific operational errors for retry
            logging.warning(f"DB connection attempt {attempt + 1} failed: {e}")
            last_exception = e
            time.sleep(DB_RETRY_DELAY)
    logging.error(f"All {DB_CONNECTION_RETRIES} DB connection attempts failed.")
    if last_exception:
        raise last_exception # Re-raise the last caught operational error
    raise psycopg2.OperationalError("Failed to connect to database after multiple retries (unknown reason if last_exception is None)")


def create_table_if_not_exists():
    # Suggestion: Add docstring
    """Creates the csp_violations table if it doesn't already exist."""
    conn = None
    try:
        # Use the robust get_db_connection here
        conn = get_db_connection() # This will retry
        with conn.cursor() as cur: # Use context manager for cursor
            cur.execute("""
                CREATE TABLE IF NOT EXISTS csp_violations (
                    id SERIAL PRIMARY KEY,
                    received_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    document_uri TEXT,
                    effective_directive TEXT,
                    original_policy TEXT,
                    violated_directive TEXT,
                    blocked_uri TEXT,
                    status_code INTEGER,
                    source_file TEXT,
                    line_number INTEGER,
                    column_number INTEGER,
                    referrer TEXT,
                    script_sample TEXT,
                    raw_report JSONB
                );
            """)
            conn.commit()
        logging.info("Table 'csp_violations' checked/created successfully.")
        return True # Indicate success
    except psycopg2.Error as e: # Catch all psycopg2 errors
        logging.error(f"Error creating/checking table 'csp_violations': {e}", exc_info=True)
        return False # Indicate failure
    finally:
        if conn:
            conn.close()

def process_report_message(ch, method, properties, body):
    # Suggestion: Add docstring
    """Processes a single CSP report message from RabbitMQ."""
    report_identifier = f"delivery_tag: {method.delivery_tag}, approx_body_start: {body[:60]}"
    logging.info(f"Processing report: {report_identifier}")

    try:
        raw_report_str = body.decode('utf-8')
        report_json = json.loads(raw_report_str)
        csp_data = report_json.get('csp-report', report_json)

        conn = None
        try:
            # Use the robust get_db_connection here
            conn = get_db_connection() # This will retry
            with conn.cursor() as cur: # Use context manager for cursor
                # Potential for type conversion errors here if csp_data.get() returns unexpected types
                # For example, if line_number is a string "None" instead of an int or None.
                # Consider adding explicit type checks or conversions with error handling if this becomes an issue.
                line_num = csp_data.get('line-number')
                col_num = csp_data.get('column-number')
                status_code = csp_data.get('status-code')

                try:
                    line_num = int(line_num) if line_num is not None else None
                except (ValueError, TypeError):
                    logging.warning(f"Invalid line_number '{line_num}' for report {report_identifier}. Setting to NULL.")
                    line_num = None
                # Similar for col_num and status_code

                cur.execute("""
                    INSERT INTO csp_violations (
                        document_uri, effective_directive, original_policy,
                        violated_directive, blocked_uri, status_code, source_file,
                        line_number, column_number, referrer, script_sample, raw_report
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """, (
                    csp_data.get('document-uri'), csp_data.get('effective-directive'),
                    csp_data.get('original-policy'), csp_data.get('violated-directive'),
                    csp_data.get('blocked-uri'), status_code, # Use sanitized status_code
                    csp_data.get('source-file'), line_num, # Use sanitized line_num
                    col_num, # Use sanitized col_num
                    csp_data.get('referrer'), csp_data.get('script-sample'),
                    raw_report_str
                ))
                conn.commit()
            logging.info(f"Successfully stored report in PostgreSQL: {report_identifier}")
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except psycopg2.Error as db_err: # Catch specific psycopg2 errors
            logging.error(f"Database Error for report {report_identifier}: {db_err}", exc_info=True)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False) # To DLQ eventually
        except Exception as e_db_conn: # Catch errors from get_db_connection() if it exhausted retries
            logging.error(f"Failed to get DB connection for report {report_identifier}: {e_db_conn}", exc_info=True)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True) # Requeue if can't connect, RabbitMQ might be up
            time.sleep(DB_RETRY_DELAY) # Give some time before broker redelivers
        finally:
            if conn:
                conn.close()

    except json.JSONDecodeError as json_err:
        logging.error(f"JSON Decode Error for report {report_identifier}. Discarding message: {body}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False) # To DLQ
    except UnicodeDecodeError as ude_err:
        logging.error(f"Unicode Decode Error for report {report_identifier}. Discarding message (raw bytes): {body!r}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False) # To DLQ
    except Exception as e:
        logging.error(f"Unexpected error processing report {report_identifier}: {e}", exc_info=True)
        # Suggestion: Change requeue to False to prevent infinite loops for unknown persistent errors.
        # If this message is truly problematic due to an unexpected unhandled case,
        # requeueing will just make it fail again. Send to DLQ.
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False) # SAFER: To DLQ for unexpected errors

def main():
    # Suggestion: Add docstring
    """Main worker function: sets up DB, connects to RabbitMQ, and starts consuming messages."""
    logging.info("Worker process starting...")

    # Robust table creation: Retry or exit if table can't be created
    table_ready = False
    for attempt in range(DB_CONNECTION_RETRIES): # Use same retry logic as get_db_connection
        if create_table_if_not_exists():
            table_ready = True
            break
        logging.warning(f"Table creation attempt {attempt + 1} failed. Retrying in {DB_RETRY_DELAY}s...")
        time.sleep(DB_RETRY_DELAY)

    if not table_ready:
        logging.critical("Failed to create or verify database table after multiple attempts. Worker exiting.")
        return # Exit if DB table setup fails

    connection = None
    while True:
        try:
            credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
            connection_params = pika.ConnectionParameters(
                host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials,
                heartbeat=600, blocked_connection_timeout=300
            )
            with pika.BlockingConnection(connection_params) as connection: # Use context manager for connection
                logging.info("Successfully connected to RabbitMQ.")
                with connection.channel() as channel: # Use context manager for channel
                    channel.queue_declare(queue=QUEUE_NAME, durable=True)
                    channel.basic_qos(prefetch_count=1) # Still good for one-by-one processing
                    channel.basic_consume(queue=QUEUE_NAME, on_message_callback=process_report_message)
                    logging.info(f"Worker consuming messages on queue: {QUEUE_NAME}. Waiting for messages...")
                    channel.start_consuming()
        except pika.exceptions.AMQPConnectionError as amqp_err:
            logging.warning(f"RabbitMQ Connection Error: {amqp_err}. Retrying in {RABBITMQ_RETRY_DELAY if 'RABBITMQ_RETRY_DELAY' in locals() else 10}s...")
        except pika.exceptions.AMQPChannelError as amqp_chan_err:
            logging.error(f"RabbitMQ Channel Error (will attempt to reconnect): {amqp_chan_err}", exc_info=True)
        except Exception as e: # Catch-all for other unexpected errors in the main loop
            logging.error(f"Worker main loop encountered an unexpected error: {e}. Retrying connection...", exc_info=True)
        # finally:
            # No need for explicit connection.close() if using 'with' statement for BlockingConnection
            # However, the loop will retry, so the old 'finally' with logging and sleep is still relevant if not using 'with'
        logging.info("Attempting to reconnect to RabbitMQ after a delay...")
        time.sleep(10) # Use a configurable delay, e.g., RABBITMQ_RETRY_DELAY

if __name__ == '__main__':
    main()