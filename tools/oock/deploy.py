#!/usr/bin/env python3

import sys

from cheroot import wsgi
from chart_server import app, start_services

def main():
  if len(sys.argv) != 3:
    print("Usage: python3 deploy.py <data_sources_spec> <page_spec>")
    exit()

  data_sources_spec = sys.argv[1]
  page_spec = sys.argv[2]

  # Launch the page and data services
  data_service_proc, page_service_proc = \
      start_services(data_sources_spec, page_spec)

  dispatcher = wsgi.WSGIPathInfoDispatcher({'/': app})
  server = wsgi.WSGIServer(('0.0.0.0', 8080), wsgi_app=dispatcher)

  try:
    server.start()
  except KeyboardInterrupt:
    server.stop()
    data_service_proc.terminate()
    page_service_proc.terminate()

########################################

if __name__ == '__main__':
  main()
