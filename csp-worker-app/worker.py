import pika
import json
import psycopg2
import time
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# RabbitMQ Configuration
RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'rabbitmq')
RABBITMQ_PORT = int(os.environ.get('RABBITMQ_PORT', 5672))
RABBITMQ_USER = os.environ.get('RABBITMQ_USER', 'user')
RABBITMQ_PASS = os.environ.get('RABBITMQ_PASS', 'password')
QUEUE_NAME = os.environ.get('QUEUE_NAME', 'csp_reports_queue')

# PostgreSQL Configuration
DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_PORT = os.environ.get('DB_PORT', 5432)
DB_NAME = os.environ.get('DB_NAME', 'csp_reports_db')
DB_USER = os.environ.get('DB_USER', 'csp_user')
DB_PASS = os.environ.get('DB_PASS', 'csp_pass')

def get_db_connection():
    logging.debug(f"Attempting to connect to PostgreSQL: host={DB_HOST}, db={DB_NAME}, user={DB_USER}")
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)

def create_table_if_not_exists():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
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
        cur.close()
        logging.info("Table 'csp_violations' checked/created successfully.")
    except Exception as e:
        logging.error(f"Error creating/checking table 'csp_violations': {e}", exc_info=True)
        # Depending on the error, you might want to retry or exit
    finally:
        if conn:
            conn.close()

def process_report_message(ch, method, properties, body):
    logging.info(f"Received report message from queue (first 100 bytes): {body[:100]}")
    try:
        # Store the raw report as JSONB and also extract common fields
        raw_report_str = body.decode('utf-8')
        report_json = json.loads(raw_report_str)

        # CSP reports often have a root key like "csp-report"
        csp_data = report_json.get('csp-report', report_json)

        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO csp_violations (
                    document_uri, effective_directive, original_policy,
                    violated_directive, blocked_uri, status_code, source_file,
                    line_number, column_number, referrer, script_sample, raw_report
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """, (
                csp_data.get('document-uri'),
                csp_data.get('effective-directive'),
                csp_data.get('original-policy'),
                csp_data.get('violated-directive'),
                csp_data.get('blocked-uri'),
                csp_data.get('status-code'),
                csp_data.get('source-file'),
                csp_data.get('line-number'), # Ensure these are converted to int if needed
                csp_data.get('column-number'), # Ensure these are converted to int if needed
                csp_data.get('referrer'),
                csp_data.get('script-sample'),
                raw_report_str # Store the full original report
            ))
            conn.commit()
            cur.close()
            logging.info("Successfully stored report in PostgreSQL.")
            ch.basic_ack(delivery_tag=method.delivery_tag) # Acknowledge message
        except Exception as db_err:
            logging.error(f"Database Error: {db_err}", exc_info=True)
            # Negative acknowledgement, requeue=False to avoid poison pill messages in simple setup
            # For production, consider a dead-letter queue.
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        finally:
            if conn:
                conn.close()

    except json.JSONDecodeError as json_err:
        logging.error(f"JSON Decode Error: {json_err}. Discarding message: {body}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    except Exception as e:
        logging.error(f"Unexpected error processing report message: {e}", exc_info=True)
        # Requeue for potentially transient errors, but be cautious of infinite loops
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        time.sleep(5) # Wait before potential retry if requeued

def main():
    # Ensure table exists before starting to consume
    create_table_if_not_exists()

    connection = None
    while True: # Keep trying to connect to RabbitMQ
        try:
            credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
            connection_params = pika.ConnectionParameters(
                host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials,
                heartbeat=600, blocked_connection_timeout=300
            )
            connection = pika.BlockingConnection(connection_params)
            channel = connection.channel()
            channel.queue_declare(queue=QUEUE_NAME, durable=True)
            # Process one message at a time per worker for simplicity in error handling
            # For higher throughput, you can increase prefetch_count, but ensure idempotency or careful error handling
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=QUEUE_NAME, on_message_callback=process_report_message)
            logging.info(f"Worker started, waiting for messages on queue: {QUEUE_NAME}")
            channel.start_consuming()
        except pika.exceptions.AMQPConnectionError as amqp_err:
            logging.error(f"RabbitMQ Connection Error: {amqp_err}. Retrying in 10 seconds...")
        except Exception as e:
            logging.error(f"Worker main loop error: {e}. Retrying in 10 seconds...", exc_info=True)
        finally:
            if connection and connection.is_open:
                connection.close()
            logging.info("RabbitMQ connection closed. Attempting to reconnect...")
            time.sleep(10)

if __name__ == '__main__':
    main()