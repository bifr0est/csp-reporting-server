from flask import Flask, request
import pika
import os
import logging

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# RabbitMQ Configuration from environment variables or defaults
RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'rabbitmq')
RABBITMQ_PORT = int(os.environ.get('RABBITMQ_PORT', 5672))
RABBITMQ_USER = os.environ.get('RABBITMQ_USER', 'user')
RABBITMQ_PASS = os.environ.get('RABBITMQ_PASS', 'password')
QUEUE_NAME = os.environ.get('QUEUE_NAME', 'csp_reports_queue')

def get_rabbitmq_connection():
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    return pika.BlockingConnection(
        pika.ConnectionParameters(host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials)
    )

@app.route('/csp-report', methods=['POST'])
def receive_csp_report():
    content_type = request.content_type
    logging.info(f"Received request with Content-Type: {content_type}")

    if content_type == 'application/csp-report' or \
       content_type == 'application/json' or \
       content_type == 'text/plain': # Some browsers might send as text/plain initially
        try:
            report_data_bytes = request.get_data()
            if not report_data_bytes:
                logging.warning("Received empty report data.")
                return 'Empty report data', 400

            logging.info(f"Attempting to publish report (first 100 bytes): {report_data_bytes[:100]}")

            connection = None
            try:
                connection = get_rabbitmq_connection()
                channel = connection.channel()
                # Declare queue (idempotent) with durability
                channel.queue_declare(queue=QUEUE_NAME, durable=True)

                channel.basic_publish(
                    exchange='',
                    routing_key=QUEUE_NAME,
                    body=report_data_bytes,
                    properties=pika.BasicProperties(
                        delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE # Make message persistent
                    ))
                logging.info(f"Successfully published report to RabbitMQ queue: {QUEUE_NAME}")
            except Exception as e:
                logging.error(f"Error sending report to RabbitMQ: {e}", exc_info=True)
                return 'Error queueing report', 500
            finally:
                if connection and connection.is_open:
                    connection.close()
                    logging.info("RabbitMQ connection closed.")

            return '', 204 # No Content - successfully received and queued
        except Exception as e:
            logging.error(f"Error processing request: {e}", exc_info=True)
            return 'Internal Server Error processing request', 500
    else:
        logging.warning(f"Unsupported Media Type: {content_type}")
        return 'Unsupported Media Type', 415

if __name__ == '__main__':
    # This is for local dev without Gunicorn. Docker CMD will use Gunicorn.
    app.run(host='0.0.0.0', port=5000, debug=False) # debug=False for Gunicorn-like behavior