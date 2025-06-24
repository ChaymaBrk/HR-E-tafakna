# Use the official Python 3.12 image as the base
FROM python:3.12.2


# Set working directory inside the container
WORKDIR /app


RUN pip install --upgrade pip
# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    locales \
    libxml2-dev \
    libxslt1-dev \
    libpoppler-cpp-dev \
    && locale-gen fr_FR.UTF-8 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*




# Set environment variables for locale
ENV LANG fr_FR.UTF-8
ENV LANGUAGE fr_FR:fr
ENV LC_ALL fr_FR.UTF-8

# Copy requirements.txt for Python dependencies
COPY requirements.txt .

# Install Python dependencies from requirements.txt
RUN pip install --no-cache-dir -r requirements.txt



# Copy all application files into the container
COPY . .

# Expose the port for Flask application
EXPOSE 8000

# Set default command to run the Flask app
CMD ["python", "app.py"]