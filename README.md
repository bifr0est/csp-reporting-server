# 🛡️ CSP Reporting Server

A production-ready, dockerized solution for receiving, processing, and storing Content Security Policy (CSP) violation reports.

## 🏗️ Architecture

```
CSP Reports → Nginx (SSL/TLS) → Flask App → RabbitMQ Cluster → Worker → Elasticsearch
```

### Components:
- **Nginx**: Reverse proxy with rate limiting and SSL termination
- **Flask App**: CSP report receiver and validator
- **RabbitMQ Cluster**: Message queue (3-node cluster for HA)
- **Worker**: Processes and forwards reports to storage
- **Elasticsearch**: Storage and indexing for CSP violation data
- **Kibana**: Data visualization dashboard

## 🚀 Quick Start

### 1. Setup Environment
```bash
# Copy and configure environment variables
cp example.env .env
# Edit .env with your settings
```

### 2. Start Services
```bash
# Start all services
docker-compose up -d

# Check status
docker-compose ps
```

### 3. Test CSP Endpoint
```bash
# Send test report
curl -k -X POST https://localhost/csp-report \
  -H "Content-Type: application/csp-report" \
  -d @test_payloads/csp_report_payload.json
```

## 🔐 Production SSL Setup (TLS-ALPN-01)

### 1. Configure Domain
```bash
# Update .env file
DOMAIN_NAME=csp-reports.yourcompany.com
CERTBOT_EMAIL=admin@yourcompany.com
```

### 2. Generate SSL Certificate
```bash
# Stop nginx temporarily
docker-compose stop nginx

# Generate Let's Encrypt certificate
docker-compose --profile certbot run --rm certbot

# Start nginx with SSL
docker-compose up -d nginx
```

### 3. Setup Auto-Renewal
```bash
# Add to crontab
0 2 * * * cd /path/to/project && docker-compose stop nginx && docker-compose --profile certbot run --rm certbot renew --quiet && docker-compose up -d nginx
```

## 🏢 External Elasticsearch Integration

To send CSP reports to an external Elasticsearch cluster instead of the local one:

```bash
# Update .env file
ELASTICSEARCH_HOSTS=https://your-external-es-cluster.com:9200

# If authentication is required:
ELASTICSEARCH_USERNAME=your_username
ELASTICSEARCH_PASSWORD=your_password
# or
ELASTICSEARCH_API_KEY=your_api_key

# For SSL/TLS with custom certificates:
ELASTICSEARCH_CA_CERTS=/path/to/ca-certificates.pem
```


## 📊 Monitoring & Analytics

### Access Points:
- **CSP Endpoint**: `https://your-domain/csp-report`
- **Health Check**: `https://your-domain/health`
- **Kibana Dashboard**: `http://localhost:5601`
- **RabbitMQ Management**: `http://localhost:15672`
- **Elasticsearch API**: `http://localhost:9200`

### Sample Queries:
```bash
# View all violations
curl "http://localhost:9200/csp-violations-*/_search?pretty"

# Filter by environment
curl "http://localhost:9200/csp-violations-*/_search" -d '{
  "query": {
    "wildcard": {
      "document-uri": "*staging.company.com*"
    }
  }
}'
```

## 📁 Project Structure

```
csp-reporting-server/
├── docker-compose.yml          # Main orchestration
├── example.env                 # Configuration template
├── .gitignore                  # Git ignore rules
├── nginx/
│   ├── nginx.conf              # Production nginx config
│   └── certs/                  # SSL certificates directory
├── csp-receiver-app/           # Flask CSP receiver
├── csp-worker-app/             # Message processor
├── rabbitmq/                   # RabbitMQ configuration
├── test_payloads/              # Sample CSP reports
└── elastic_template.json       # Elasticsearch mapping
```

## 🔧 Configuration Files

### `example.env`
Complete environment variable template with:
- RabbitMQ cluster settings
- Elasticsearch configuration
- SSL/TLS settings
- Performance tuning parameters

### `elastic_template.json`
Elasticsearch index template for optimal CSP data storage and search performance.

## 🚨 Security Features

- **Rate Limiting**: 5 req/sec per IP, burst up to 10
- **SSL/TLS Encryption**: Modern cipher suites, TLS 1.2+
- **Security Headers**: HSTS, XSS protection, content sniffing prevention
- **Input Validation**: CSP report format validation
- **Request Size Limits**: 2MB max per request

## 🎯 Use Cases

### Multi-Environment Support
All environments can send to the same endpoint - filter by `document-uri` in Elasticsearch:
```javascript
// Production CSP policy
<meta http-equiv="Content-Security-Policy" 
      content="default-src 'self'; report-uri https://csp-reports.company.com/csp-report">

// Staging CSP policy  
<meta http-equiv="Content-Security-Policy" 
      content="default-src 'self'; report-uri https://csp-reports.company.com/csp-report">
```

### Data Flow
1. **Browser** sends CSP violation to `/csp-report`
2. **Nginx** applies rate limiting and forwards to Flask
3. **Flask app** validates and queues report in RabbitMQ
4. **Worker** processes queue and sends to Elasticsearch
5. **Data** available for analysis in Kibana

## 🤝 Contributing

1. Copy `example.env` to `.env`
2. Modify settings as needed
3. Test locally with `docker-compose up -d`
4. Submit PRs for improvements

## 📜 License

MIT License

Copyright (c) 2025 bifr0est

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.