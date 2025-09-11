#!/bin/bash

# Test script to verify IP allowlisting is working
# This should return 403 when run from an unauthorized IP

DOMAIN=${1:-localhost}

echo "Testing CSP endpoint security from unauthorized IP..."
echo "Domain: $DOMAIN"
echo ""

# Test unauthorized request (should get 403)
echo "=== Testing POST from unauthorized IP (should return 403) ==="
curl -v -X POST "https://$DOMAIN/csp-report" \
  -H "Content-Type: application/csp-report" \
  -d @csp_report_payload.json \
  -w "\nHTTP Status: %{http_code}\n" 2>&1 | grep -E "(HTTP|403|Forbidden)"

echo ""
echo "=== Testing health endpoint (should work from any IP) ==="
curl -v "https://$DOMAIN/health" \
  -w "\nHTTP Status: %{http_code}\n" 2>&1 | grep -E "(HTTP|200|OK)"

echo ""
echo "Expected results:"
echo "- CSP endpoint: 403 Forbidden (if IP not allowlisted)"
echo "- Health endpoint: 200 OK (always accessible)"