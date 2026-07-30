FROM python:3.11-slim

# Create a non-root user for Kubernetes security best practices
RUN useradd -m -s /bin/bash scaler_user
WORKDIR /app

# Force Python to print logs immediately to stdout
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scaler.py .

# Drop root privileges
USER scaler_user

# Execute the continuous control loop
CMD ["python", "scaler.py"]