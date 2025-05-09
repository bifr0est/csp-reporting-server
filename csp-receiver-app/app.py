from flask import Flask, request
import pika
import os
import logging
import time

app = Flask(__name__)

# --- Configuration ---
# Log Level from environment, defaulting to INFO
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()

# RabbitMQ Configuration from environment variables or defaults
RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'rabbitmq')
RABBITMQ_PORT = int(os.environ.get('RABBITMQ_PORT', 5672)) # Consider try-except for int conversion
RABBITMQ_USER = os.environ.get('RABBITMQ_USER', 'user')
RABBITMQ_PASS = os.environ.get('RABBITMQ_PASS', 'password')
QUEUE_NAME = os.environ.get('QUEUE_NAME', 'csp_reports_queue')

# Pika's own connection retry parameters
RABBITMQ_CONNECTION_ATTEMPTS = int(os.environ.get('RABBITMQ_CONNECTION_ATTEMPTS', 3))
RABBITMQ_RETRY_DELAY = int(os.environ.get('RABBITMQ_RETRY_DELAY', 5)) # Pika's internal retry delay in seconds

# --- Logging Setup ---
logging.basicConfig(level=LOG_LEVEL,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(message)s')

# --- RabbitMQ Connection ---
def get_rabbitmq_connection():
    """
    Establishes and returns a blocking connection to RabbitMQ.
    Uses Pika's built-in retry mechanism for initial connection.
    """
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    parameters = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        credentials=credentials,
        connection_attempts=RABBITMQ_CONNECTION_ATTEMPTS,
        retry_delay=RABBITMQ_RETRY_DELAY
    )
    try:
        connection = pika.BlockingConnection(parameters)
        logging.info(f"Successfully connected to RabbitMQ on attempt.") # Pika handles actual attempt logging
        return connection
    except pika.exceptions.AMQPConnectionError as e:
        logging.error(f"Failed to connect to RabbitMQ after {RABBITMQ_CONNECTION_ATTEMPTS} attempts: {e}", exc_info=True)
        raise # Re-raise the exception to be handled by the caller

# --- Flask Routes ---
@app.route('/csp-report', methods=['POST'])
def receive_csp_report():
    """
    Receives CSP violation reports via POST request and publishes them to a RabbitMQ queue.
    """
    content_type = request.content_type
    # You could add a request ID for better tracing in a distributed system
    # request_id = request.headers.get('X-Request-ID', secrets.token_hex(4))
    # logging.info(f"RID-{request_id}: Received request with Content-Type: {content_type}")
    logging.info(f"Received request with Content-Type: {content_type}")

    if not (content_type == 'application/csp-report' or \
            content_type == 'application/json' or \
            content_type == 'text/plain'): # Some browsers might send as text/plain initially
        logging.warning(f"Unsupported Media Type: {content_type}")
        return 'Unsupported Media Type', 415

    try:
        report_data_bytes = request.get_data()
        if not report_data_bytes:
            logging.warning("Received empty report data.")
            return 'Empty report data', 400

        logging.info(f"Attempting to publish report (first 100 bytes): {report_data_bytes[:100]}")

        try:
            # Using 'with' statement for BlockingConnection ensures it's closed.
            with get_rabbitmq_connection() as connection:
                # Channel should also ideally be a context manager if pika version supports it well,
                # or closed in a finally block if opened separately.
                # For BlockingConnection, channel ops are usually on the same connection lifetime.
                channel = connection.channel()
                channel.queue_declare(queue=QUEUE_NAME, durable=True)

                channel.basic_publish(
                    exchange='',
                    routing_key=QUEUE_NAME,
                    body=report_data_bytes,
                    properties=pika.BasicProperties(
                        delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE
                    ))
                logging.info(f"Successfully published report to RabbitMQ queue: {QUEUE_NAME}")

        except pika.exceptions.AMQPConnectionError as e:
            logging.error(f"RabbitMQ connection error during publish: {e}", exc_info=True)
            # 503 Service Unavailable is appropriate if the message bus is down
            return 'Message queueing service unavailable', 503
        except pika.exceptions.AMQPChannelError as e:
            logging.error(f"RabbitMQ channel error during publish: {e}", exc_info=True)
            return 'Error establishing channel with queueing service', 500
        except Exception as e: # Catch other Pika errors or general errors during publish
            logging.error(f"Unexpected error sending report to RabbitMQ: {e}", exc_info=True)
            return 'Error queueing report', 500

        return '', 204 # No Content - successfully received and queued

    except Exception as e: # This catches errors like request.get_data() failing
        logging.error(f"Error processing request (pre-queue operation): {e}", exc_info=True)
        return 'Internal Server Error processing request', 500

if __name__ == '__main__':
    # This block is for direct execution (python app.py)
    # Gunicorn will import the `app` object directly.
    # Not setting debug=True here as Gunicorn is preferred for dev/prod.
    app.run(host='0.0.0.0', port=5000)