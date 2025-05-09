FROM python:3.13-alpine 

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Use Gunicorn for a more production-like WSGI server
# Exposing on port 5000, which docker-compose will map or Nginx will connect to.
# Increase workers based on your load and CPU cores available to the container.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]