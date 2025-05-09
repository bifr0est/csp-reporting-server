from flask import Flask, render_template, request
import psycopg2
import psycopg2.extras
import os
import logging
from datetime import timezone

app = Flask(__name__)

# --- Configuration --- (Keep as is)
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_PORT = int(os.environ.get('DB_PORT', 5432))
DB_NAME = os.environ.get('DB_NAME', 'csp_reports_db')
DB_USER = os.environ.get('DB_USER', 'csp_user')
DB_PASS = os.environ.get('DB_PASS', 'csp_pass')

# --- Logging Setup --- (Keep as is)
logging.basicConfig(level=LOG_LEVEL,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(message)s')

# --- Allowed sortable columns and their actual DB column names ---
ALLOWED_SORT_COLUMNS = {
    "id": "id",
    "received_at": "received_at",
    "violated_directive": "violated_directive",
    "document_uri": "document_uri",   # <-- ADDED
    "blocked_uri": "blocked_uri"     # <-- ADDED
}

def get_db_connection():
    """Establishes and returns a connection to PostgreSQL."""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS
        )
        logging.info("Successfully connected to PostgreSQL for dashboard.")
        return conn
    except psycopg2.OperationalError as e:
        logging.error(f"Dashboard failed to connect to PostgreSQL: {e}", exc_info=True)
        raise

@app.route('/')
def index():
    """Fetches CSP reports from the database and renders them, with filtering and sorting."""
    reports = []
    db_error = None
    conn = None

    # Get filter criteria
    violated_directive_filter = request.args.get('violated_directive_filter', None)

    # Get sort criteria
    sort_by_param = request.args.get('sort_by', 'received_at') # Default sort column
    sort_order_param = request.args.get('sort_order', 'desc').lower() # Default sort order

    # Validate sort parameters
    if sort_by_param not in ALLOWED_SORT_COLUMNS:
        logging.warning(f"Invalid sort_by parameter: {sort_by_param}. Defaulting to 'received_at'.")
        sort_by_param = 'received_at'
    
    db_column_to_sort = ALLOWED_SORT_COLUMNS[sort_by_param]

    if sort_order_param not in ['asc', 'desc']:
        logging.warning(f"Invalid sort_order parameter: {sort_order_param}. Defaulting to 'desc'.")
        sort_order_param = 'desc'

    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            base_sql_query = """
                SELECT id, received_at, document_uri, effective_directive, 
                       original_policy, violated_directive, blocked_uri, 
                       status_code, source_file, line_number, column_number, 
                       referrer, script_sample, raw_report 
                FROM csp_violations 
            """
            
            conditions = []
            query_params = []

            if violated_directive_filter:
                conditions.append("violated_directive ILIKE %s")
                query_params.append(f"%{violated_directive_filter}%")

            if conditions:
                base_sql_query += " WHERE " + " AND ".join(conditions)
            
            order_by_clause = f"ORDER BY {db_column_to_sort} {sort_order_param.upper()}"
            
            final_sql_query = f"{base_sql_query} {order_by_clause} LIMIT 50;"
            
            logging.debug(f"Executing SQL: {final_sql_query} with params: {query_params}")
            cur.execute(final_sql_query, tuple(query_params))
            reports = cur.fetchall()
            
            for report in reports:
                if report.get('received_at') and report['received_at'].tzinfo is None:
                    report['received_at'] = report['received_at'].replace(tzinfo=timezone.utc)

    except psycopg2.Error as e:
        logging.error(f"Database error while fetching reports for dashboard: {e}", exc_info=True)
        db_error = str(e)
    except Exception as e:
        logging.error(f"An unexpected error occurred while fetching reports: {e}", exc_info=True)
        db_error = "An unexpected error occurred."
    finally:
        if conn:
            conn.close()
            logging.debug("Dashboard DB connection closed.")

    return render_template('index.html', 
                           reports=reports, 
                           error=db_error, 
                           current_filter_vd=violated_directive_filter,
                           current_sort_by=sort_by_param,
                           current_sort_order=sort_order_param)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=(LOG_LEVEL == 'DEBUG'))