#! /usr/bin/env python

import sys, os, re, requests, json, dateutil.parser, time
import Evtx.Evtx as evtx, xml.etree.ElementTree as ET
from Queue import Queue
from threading import Thread

# Configure ElasticNode URLs for ingest
nodes = []
if "PRIMARY_INGEST_NODE" in os.environ:
	nodes.append(os.getenv("PRIMARY_INGEST_NODE"))
else:
	print "ENV PRIMARY_INGEST_NODE not specified. Exiting."
	sys.exit()
if "SECONDARY_INGEST_NODE" in os.environ:
	nodes.append(os.getenv("SECONDARY_INGEST_NODE"))
if "TERTIARY_INGEST_NODE" in os.environ:
	nodes.append(os.getenv("TERTIARY_INGEST_NODE"))

# Configure log buffer size
logBufferSize = os.getenv("LOG_BUFFER_SIZE", "1000")

# Configure index; default "offline_winevt"
index = os.getenv("EVTX_INDEX", "offline_winevt")

# If index doesn't exist, create it
currentNode = 0
success = False
while not success:
	try:
		if not requests.head("http://" + nodes[0] + "/" + index).ok:
			print "Creating index..."
			mappings = {
				"mappings": {
					"doc": {
						"properties": {
							"System": {
								"type": "object",
								"properties": {
									"TimeCreated.SystemTime": {
										"type": "date",
										"format": "date_hour_minute_second"
									},
									"Channel.Value": {
										"type": "keyword"
									},
									"Computer.Value": {
										"type": "keyword"
									},
									"EventID.Qualifiers": {
										"type": "integer"
									},
									"EventID.Value": {
										"type": "integer"
									},
									"EventRecordID.Value": {
										"type": "integer"
									},
									"Keywords.Value": {
										"type": "text"
									},
									"Level.Value": {
										"type": "integer"
									},
									"Provider.Name": {
										"type": "keyword"
									},
									"Provider.Value": {
										"type": "text"
									},
									"Security.UserID": {
										"type": "text"
									},
									"Security.Value": {
										"type": "integer"
									},
									"Task.Value": {
										"type": "integer"
									}
								}
							},
							"EventData": {
								"type": "nested",
								"properties": {
									"Data": {
										"type": "object",
										"properties": {
											"Name": {
												"type": "keyword"
											},
											"Value": {
												"type": "keyword"
											}
										}
									}
								}
							}
						}
					}
				}
			}
			res = requests.put("http://" + nodes[0] + "/" + index, data=json.dumps(mappings), headers={"Content-Type": "application/json"})
			if res.ok:
				print "OK."
			else:
				print json.dumps(res.json())
		success = True
	except:
		# Move to the next node; loop back if tried all available
		currentNode = (currentNode + 1) % len(nodes)
		print "Connection timeout; attempting on node: " + nodes[currentNode]
	

# Function called by threading
# Handles posting information to ElasticSearch using python requests
def postToElastic():
	# Worker waits for job
	while True:
		try:
			# Store results in buffer to ensure it does not get overwritten while processing
			logBuffer = q.get()

			# Start the node fault-tolerance
			currentNode = 0
			
			# Continuously attempt to post to nodes; when one fails, try the next if listed
			success = False
			results = {}
			while not success:
				try:		
					# Post to current node
					results = requests.post("http://" + nodes[currentNode] + "/" + index + "/doc/_bulk", data=logBuffer, headers={"content-type": "application/x-ndjson"})
					success = True
				except:
					# Move to the next node; loop back if tried all available
					currentNode = (currentNode + 1) % len(nodes)
					print "Connection timeout; attempting on node: " + nodes[currentNode]

			# Print results to stdout
			if not results.ok:
				print json.dumps(results.json(), indent=4)
			else:
				print "+ Ok."
		except:
			print sys.exc_info()
		finally:
			# Cleanup
			q.task_done()

# Initialize the job queue; maxsize=0 means infinite size
q = Queue(maxsize=0)

# Initialize the workers with ten threads
num_threads = 10
for i in range(num_threads):
	worker = Thread(target=postToElastic)
	worker.daemon = True
	worker.start()

# Monitor for files
while True:
	print "Checking for files..."
	# Iterate through all files in shared directory, ingesting one at a time
	for file in os.listdir("evtx-monitor/"):
		# Ensure file is type evtx
		if file.lower().endswith(".evtx"):
			# Use evtx to parse the binary data
			with evtx.Evtx(os.path.join("evtx-monitor", file)) as log:
				events = ""
				print "# Ingesting " + os.path.join("evtx-monitor", file) + "..."
				logBufferLength = 0
				
				# Each "record" is a Windows event
				for record in log.records():
					event = {"System": {}, "EventData": []}
				
					# Get the root of the XML data
					root = ET.fromstring(record.xml())
				
					# Iterate through the first child, or system, items
					for system_items in root[0]:
						tag = re.search("(?<=}).*", system_items.tag).group(0)
						event["System"][tag] = {}
						for key, value in system_items.attrib.iteritems():
							if key == "SystemTime":
								event["System"][tag][key] = dateutil.parser.parse(value.split(".")[0]).isoformat()
							else:
								event["System"][tag][key] = value
						event["System"][tag]["Value"] = system_items.text
				
					# Iterate through the second child, or data, items
					for data_items in root[1]:
						newData = {}
						tag = re.search("(?<=}).*", data_items.tag).group(0)
						newData[tag] = {}
						for key, value in data_items.attrib.iteritems():
							newData[tag][key] = value
						newData[tag]["Value"] = data_items.text
						event["EventData"].append(newData)
				
					# Set up the events for bulk ingest (newlines are important)
					events = events + '{"index": {}}\n'
					events = events + json.dumps(event) + "\n"
					logBufferLength = logBufferLength + 1
					
					# Dump log buffer when full
					if logBufferLength >= int(logBufferSize):
						print "Dumping " + logBufferSize + " Logs..."
						q.put(events)
						events = ""
						logBufferLength = 0

				# Use the requests module of Python to send the data to ElasticSearch
				if events != "":
					print "Dumping remaining Logs...:"
					q.put(events)
				else:
					print "- No logs to ingest."
			
			# Wait for all jobs to complete before moving onto the next file.
			q.join()
			
			# Move file to "/evtx-monitor/ingested" when complete
			os.rename("evtx-monitor/" + file, "evtx-monitor/ingested/" + file)
	# Check again in 30 seconds
	time.sleep(30)
