from flask import Flask, request
import pika
from pika.exceptions import AMQPConnectionError, AMQPChannelError
import os
import logging
import json

app = Flask(__name__)

# --- Configuration ---
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()

# RabbitMQ Configuration
RABBITMQ_NODES_CSV = os.environ.get('RABBITMQ_NODES_CSV', 'rabbitmq:5672')
RABBITMQ_USER = os.environ.get('RABBITMQ_USER', 'user')
RABBITMQ_PASS = os.environ.get('RABBITMQ_PASS', 'password')
QUEUE_NAME = os.environ.get('QUEUE_NAME', 'csp_reports_queue')
# Pika's internal per-host connection attempt parameters
RABBITMQ_CONNECTION_ATTEMPTS_PER_NODE = int(os.environ.get('RABBITMQ_CONNECTION_ATTEMPTS_PER_NODE', 3))
RABBITMQ_RETRY_DELAY_PER_NODE = int(os.environ.get('RABBITMQ_RETRY_DELAY_PER_NODE', 5))

# --- Logging Setup ---
logging.basicConfig(level=LOG_LEVEL,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(message)s')

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
            else: # Only hostname provided
                host = node_str
                port = 5672 # Default AMQP port
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

def get_rabbitmq_connection():
    """
    Establishes and returns a blocking connection to RabbitMQ, trying a list of nodes.
    """
    connection_params_list = get_rabbitmq_connection_params_list()
    try:
        connection = pika.BlockingConnection(connection_params_list)
        # Log successful connection without accessing private attributes
        logging.info("Successfully connected to RabbitMQ cluster")
        return connection
    except AMQPConnectionError as e:
        logging.error(f"Failed to connect to any configured RabbitMQ node: {e}", exc_info=True)
        raise # Re-raise to be handled by the caller

# --- Flask Routes ---
@app.route('/csp-report', methods=['POST'])
def receive_csp_report():
    """
    Receives CSP violation reports via POST request and publishes them to a RabbitMQ queue.
    """

    content_type = request.content_type
    logging.info(f"Received request with Content-Type: {content_type}")

    # Accept common CSP report content types
    accepted_content_types = {
        "application/csp-report",
        "application/json", 
        "application/reports+json",
        "text/plain"
    }
    
    if content_type not in accepted_content_types:
        logging.warning(f"Unsupported Media Type: {content_type}")
        return "Unsupported Media Type", 415


    try:
        # Limit request size to prevent DoS attacks
        max_content_length = 10240  # 10KB limit for CSP reports
        if request.content_length and request.content_length > max_content_length:
            logging.warning(f"Request too large: {request.content_length} bytes")
            return 'Request Entity Too Large', 413

        report_data_bytes = request.get_data()
        if not report_data_bytes:
            logging.warning("Received empty report data.")
            return 'Empty report data', 400

        # Validate that the payload is valid JSON and has proper CSP structure
        try:
            report_json = json.loads(report_data_bytes.decode("utf-8"))

            # Basic JSON structure validation
            if not isinstance(report_json, dict):
                logging.warning("Invalid CSP report: not a JSON object")
                return "Invalid CSP report format", 400

            # Extract CSP data from wrapper or direct format
            if "csp-report" in report_json:
                csp_data = report_json["csp-report"]
                if not isinstance(csp_data, dict):
                    logging.warning("Invalid CSP report: csp-report field is not an object")
                    return "Invalid CSP report structure", 400
            else:
                csp_data = report_json

            # Validate required CSP fields
            required_fields = ["violated-directive", "document-uri"]
            missing_fields = [field for field in required_fields if field not in csp_data]
            
            if missing_fields:
                logging.warning(f"Invalid CSP report: missing required fields: {missing_fields}")
                return "Invalid CSP report structure", 400

            # Validate violated-directive format
            violated_directive = csp_data.get("violated-directive", "")
            if not isinstance(violated_directive, str) or not violated_directive.strip():
                logging.warning("Invalid CSP report: violated-directive must be a non-empty string")
                return "Invalid CSP report structure", 400

            # Validate document-uri format  
            document_uri = csp_data.get("document-uri", "")
            if not isinstance(document_uri, str) or not document_uri.strip():
                logging.warning("Invalid CSP report: document-uri must be a non-empty string")
                return "Invalid CSP report structure", 400

            # Validate string fields if present (can be empty)
            string_fields = ["blocked-uri", "referrer", "effective-directive", "original-policy", 
                           "disposition", "source-file", "script-sample"]
            for field in string_fields:
                if field in csp_data and not isinstance(csp_data[field], str):
                    logging.warning(f"Invalid CSP report: {field} must be a string")
                    return "Invalid CSP report structure", 400

            # Validate integer fields if present
            int_fields = ["line-number", "column-number", "status-code"]
            for field in int_fields:
                if field in csp_data and not isinstance(csp_data[field], int):
                    logging.warning(f"Invalid CSP report: {field} must be an integer")
                    return "Invalid CSP report structure", 400

            # Limit object depth to prevent deeply nested JSON attacks
            def check_json_depth(obj, max_depth=5, current_depth=0):
                if current_depth > max_depth:
                    return False
                if isinstance(obj, dict):
                    return all(check_json_depth(v, max_depth, current_depth + 1) for v in obj.values())
                elif isinstance(obj, list):
                    return all(check_json_depth(item, max_depth, current_depth + 1) for item in obj)
                return True

            if not check_json_depth(report_json):
                logging.warning("Invalid CSP report: JSON structure too deeply nested")
                return "Invalid CSP report structure", 400
                
        except json.JSONDecodeError as e:
            logging.warning(f"Invalid JSON in CSP report: {e}")
            return 'Invalid JSON format', 400
        except UnicodeDecodeError as e:
            logging.warning(f"Invalid UTF-8 encoding in CSP report: {e}")
            return 'Invalid character encoding', 400

        logging.info(f"Attempting to publish valid CSP report (size: {len(report_data_bytes)} bytes)")

        try:
            with get_rabbitmq_connection() as connection:
                channel = connection.channel()
                channel.queue_declare(
                    queue=QUEUE_NAME, 
                    durable=True, 
                    arguments={'x-queue-type': 'quorum'}
                )
                channel.basic_publish(
                    exchange='',
                    routing_key=QUEUE_NAME,
                    body=report_data_bytes,
                    properties=pika.BasicProperties(
                        delivery_mode=2  # Persistent delivery
                    ))
                logging.info(f"Successfully published report to RabbitMQ queue: {QUEUE_NAME}")

        except AMQPConnectionError as e:
            logging.error(f"RabbitMQ connection error during publish: {e}", exc_info=True)
            return 'Message queueing service unavailable', 503
        except AMQPChannelError as e:
            logging.error(f"RabbitMQ channel error during publish: {e}", exc_info=True)
            return 'Error establishing channel with queueing service', 500
        except Exception as e: # Catch other errors from get_rabbitmq_connection or publish
            logging.error(f"Unexpected error sending report to RabbitMQ: {e}", exc_info=True)
            return 'Error queueing report', 500

        return '', 204

    except Exception as e: # Catches errors like request.get_data() failing
        logging.error(f"Error processing request (pre-queue operation): {e}", exc_info=True)
        return 'Internal Server Error processing request', 500

if __name__ == '__main__':
    # Gunicorn will import the `app` object directly.
    # This block is for direct execution (python app.py) for local testing if not using Gunicorn.
    app.run(host='0.0.0.0', port=5000)
