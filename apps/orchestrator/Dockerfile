FROM python:3.11-slim

# Install dependencies and copy application code.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose the HTTP port.
EXPOSE 8000

# Start the FastAPI application using uvicorn. The host and port are
# explicitly specified to bind to all interfaces.
CMD ["uvicorn", "main:app", "--host=0.0.0.0", "--port=8000"]
