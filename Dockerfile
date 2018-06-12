# Use an official Python runtime as a parent image
FROM python:2.7-slim

# Set the working directory to /app
WORKDIR /app

# Copy the current directory contents into the container at /app
ADD . /app

# Install any needed packages
RUN mv six.* /usr/local/lib/python2.7/site-packages/
RUN cd hexdump && python setup.py install
RUN cd evtx && python setup.py install
RUN mkdir /evtx-monitor

# Define environment variables
ENV PRIMARY_INGEST_NODE elasticsearch:9200

# Run python script when container launches
CMD ["python", "-u", "evtx-to-elastic.py"]
