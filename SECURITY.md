# 🔒 Security Configuration

This document outlines security configurations for the CSP Reporting Server, particularly for financial organizations and high-security environments.

## IP Allowlisting (Recommended for Financial Orgs)

The CSP endpoint can be restricted to only accept reports from your web servers using IP allowlisting.

### Configuration

1. **Set allowed IPs in your `.env` file:**
```bash
# Single IP address
CSP_ALLOWED_IPS=203.0.113.5

# Multiple IP addresses (comma-separated)
CSP_ALLOWED_IPS=203.0.113.5,198.51.100.10

# CIDR blocks for IP ranges
CSP_ALLOWED_IPS=10.0.1.0/24,192.168.1.0/24

# Mixed format
CSP_ALLOWED_IPS=203.0.113.5,10.0.1.0/24,198.51.100.10
```

2. **Restart the services:**
```bash
docker-compose down
docker-compose up -d
```

### How It Works

- **Allowed IPs**: Requests from configured IPs are processed normally
- **Blocked IPs**: All other IPs receive a `403 Forbidden` response
- **Rate limiting**: Still applies to allowed IPs (5 req/sec per IP)
- **Health check**: `/health` endpoint remains publicly accessible

### Security Benefits

✅ **Eliminates 99% of attack surface** - only your web servers can reach the endpoint  
✅ **Prevents spam/DoS attacks** from random internet sources  
✅ **Reduces analytics noise** from fake violation reports  
✅ **Compliance-friendly** for financial and regulated industries  

### Testing Configuration

```bash
# Test from allowed IP (should work)
curl -X POST https://your-domain.com/csp-report \
  -H "Content-Type: application/csp-report" \
  -d '{"csp-report":{"violated-directive":"script-src"}}'

# Test from blocked IP (should return 403)
# Use VPN or different server to test
```

## Network Architecture Recommendations

### For Financial Organizations

**Option 1: Internal Network (Most Secure)**
```
Internet → Web Servers (DMZ) → Firewall Rules → CSP Server (Internal Network)
```

**Option 2: IP Allowlisting (Good Compromise)**
```
Internet → Caddy (IP Filtered) → CSP Server (Public but Restricted)
```

### Migration Path

1. **Phase 1**: Implement IP allowlisting on current public setup
2. **Phase 2**: Plan migration to internal network when ready
3. **Phase 3**: Decommission public instance

## Additional Security Features

### Already Implemented
- **Rate Limiting**: 5 requests/sec per IP, 10 burst
- **Request Size Limits**: 10KB maximum payload
- **Content Type Validation**: Only accepts CSP report formats
- **JSON Structure Validation**: Validates CSP report structure
- **Automatic HTTPS**: Let's Encrypt SSL certificates
- **Security Headers**: HSTS, XSS protection, content sniffing prevention

### Monitoring Recommendations
- Monitor for unusual traffic patterns
- Alert on rate limit violations
- Track CSP report sources and content
- Integrate with SIEM systems for financial compliance

## Compliance Notes

This configuration helps meet security requirements for:
- **PCI DSS**: Reduces attack surface for payment card environments
- **SOX**: Provides audit-friendly access controls
- **Financial regulations**: Defense-in-depth security approach

## Troubleshooting

**403 Forbidden errors**: Check that your web server IPs are correctly configured in `CSP_ALLOWED_IPS`

**No CSP reports**: Verify your web servers can reach the CSP endpoint from their configured IPs

**Rate limiting**: Legitimate traffic may be rate-limited; consider adjusting limits if needed