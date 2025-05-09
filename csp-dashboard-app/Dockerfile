FROM python:3.13-alpine

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# We'll run this Flask app directly for simplicity, or use Gunicorn later if needed
# For a simple dashboard, Flask's dev server might be okay for many internal use cases,
# but Gunicorn is better for anything more serious. Let's start with Flask's server.
CMD ["python", "dashboard.py"]