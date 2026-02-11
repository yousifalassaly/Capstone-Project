FROM python:3.11-slim

# Install system dependencies for Ansible
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        openssh-client \
        sshpass \
        build-essential \
        libssl-dev \
        libffi-dev \
        python3-dev \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Ansible
RUN pip install --no-cache-dir "ansible-core==2.15.3"

# Install specific Ansible Galaxy collection
RUN ansible-galaxy collection install ibm.ibm_zos_core:1.8.0

# Copy application code
COPY . .

# Expose the HTTP port
EXPOSE 8000

# Start the FastAPI application
CMD ["uvicorn", "main:app", "--host=0.0.0.0", "--port=8000"]
