# Use a lightweight Python base image
FROM python:3.11-slim

# Create a non-root user for security
RUN useradd -m -s /bin/bash predictor_user

# Set the working directory
WORKDIR /app

# Copy dependency list and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the actual Python script
COPY predictor.py .

# Switch to the non-root user
USER predictor_user

# Expose the port the Prometheus client uses
EXPOSE 8000

# Run the continuous evaluation loop
CMD ["python", "predictor.py"]
