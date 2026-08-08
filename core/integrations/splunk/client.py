"""Splunk API service for data enrichment."""

import logging
import requests
from typing import Optional, List, Dict
import urllib3

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class SplunkService:
    """Service for interacting with Splunk API."""
    
    def __init__(self, server_url: str, username: str, password: str, 
                 verify_ssl: bool = False):
        """
        Initialize Splunk service.
        
        Args:
            server_url: Splunk server URL (e.g., "https://splunk.example.com:8089")
            username: Username for authentication
            password: Password for authentication
            verify_ssl: Whether to verify SSL certificates (default: False)
        """
        self.server_url = server_url.rstrip('/')
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session_key: Optional[str] = None
        
        # Set default headers
        self.session.headers.update({
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json'
        })
    
    def authenticate(self) -> bool:
        """
        Authenticate with Splunk server and get session key.
        
        Returns:
            True if authentication successful, False otherwise.
        """
        try:
            auth_url = f"{self.server_url}/services/auth/login"
            data = {
                'username': self.username,
                'password': self.password,
                'output_mode': 'json'
            }
            
            response = self.session.post(auth_url, data=data)
            
            if response.status_code == 200:
                result = response.json()
                self.session_key = result.get('sessionKey')
                if self.session_key:
                    self.session.headers.update({
                        'Authorization': f'Splunk {self.session_key}'
                    })
                    logger.info(f"Successfully authenticated to Splunk as {self.username}")
                    return True
                else:
                    logger.error("No session key returned from Splunk")
                    return False
            else:
                logger.error(f"Authentication failed: HTTP {response.status_code}")
                return False
        
        except Exception as e:
            logger.error(f"Error during authentication: {e}")
            return False
    
    def test_connection(self) -> tuple[bool, str]:
        """
        Test connection to Splunk server.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            if not self.authenticate():
                return False, "Authentication failed"
            
            # Try to get server info
            response = self.session.get(
                f"{self.server_url}/services/server/info",
                params={'output_mode': 'json'}
            )
            
            if response.status_code == 200:
                return True, "Connection successful"
            else:
                return False, f"Connection failed: HTTP {response.status_code}"
        
        except requests.exceptions.RequestException as e:
            return False, f"Connection error: {str(e)}"
    
    def search(self, query: str, earliest_time: str = "-24h", 
               latest_time: str = "now", max_count: int = 1000) -> Optional[List[Dict]]:
        """
        Execute a search query in Splunk.
        
        Args:
            query: SPL (Splunk Processing Language) query
            earliest_time: Earliest time for search (default: -24h)
            latest_time: Latest time for search (default: now)
            max_count: Maximum number of results to return
        
        Returns:
            List of result dictionaries, or None if error
        """
        try:
            if not self.session_key:
                if not self.authenticate():
                    return None
            
            # Create search job
            search_url = f"{self.server_url}/services/search/jobs"
            search_data = {
                'search': f"search {query}",
                'earliest_time': earliest_time,
                'latest_time': latest_time,
                'output_mode': 'json'
            }
            
            response = self.session.post(search_url, data=search_data)
            
            if response.status_code not in [200, 201]:
                logger.error(f"Failed to create search job: {response.status_code} - {response.text}")
                return None
            
            job_data = response.json()
            sid = job_data.get('sid')
            
            if not sid:
                logger.error("No search ID returned")
                return None
            
            logger.info(f"Created search job: {sid}")
            
            # Poll for job completion
            job_url = f"{self.server_url}/services/search/jobs/{sid}"
            max_attempts = 60  # 60 attempts with 1 second wait = 1 minute max
            
            for attempt in range(max_attempts):
                status_response = self.session.get(
                    job_url,
                    params={'output_mode': 'json'}
                )
                
                if status_response.status_code == 200:
                    job_status = status_response.json()
                    entry = job_status.get('entry', [{}])[0]
                    content = entry.get('content', {})
                    
                    is_done = content.get('isDone', False)
                    
                    if is_done:
                        # Get results
                        results_url = f"{job_url}/results"
                        results_response = self.session.get(
                            results_url,
                            params={
                                'output_mode': 'json',
                                'count': max_count
                            }
                        )
                        
                        if results_response.status_code == 200:
                            results_data = results_response.json()
                            results = results_data.get('results', [])
                            logger.info(f"Search completed with {len(results)} results")
                            
                            # Clean up job
                            self.session.delete(job_url)
                            
                            return results
                        else:
                            logger.error(f"Failed to get results: {results_response.status_code}")
                            return None
                    
                    # Wait before next poll
                    import time
                    time.sleep(1)
                else:
                    logger.error(f"Failed to check job status: {status_response.status_code}")
                    return None
            
            logger.error("Search job timed out")
            # Try to cancel the job
            self.session.delete(job_url)
            return None
        
        except Exception as e:
            logger.error(f"Error executing search: {e}")
            return None
    
    def search_by_ip(self, ip_address: str, hours: int = 24) -> Optional[List[Dict]]:
        """
        Search for events related to an IP address.
        
        Args:
            ip_address: IP address to search for
            hours: Number of hours to look back (default: 24)
        
        Returns:
            List of events or None
        """
        query = f'"{ip_address}" | head 1000'
        return self.search(query, earliest_time=f"-{hours}h")
    
    
    def search_by_hash(self, file_hash: str, hours: int = 24) -> Optional[List[Dict]]:
        """
        Search for events related to a file hash.
        
        Args:
            file_hash: File hash (MD5, SHA1, or SHA256) to search for
            hours: Number of hours to look back (default: 24)
        
        Returns:
            List of events or None
        """
        query = f'"{file_hash}" | head 1000'
        return self.search(query, earliest_time=f"-{hours}h")
    
    def search_by_username(self, username: str, hours: int = 24) -> Optional[List[Dict]]:
        """
        Search for events related to a username.
        
        Args:
            username: Username to search for
            hours: Number of hours to look back (default: 24)
        
        Returns:
            List of events or None
        """
        query = f'user="{username}" OR username="{username}" OR account="{username}" | head 1000'
        return self.search(query, earliest_time=f"-{hours}h")
    
    def search_by_hostname(self, hostname: str, hours: int = 24) -> Optional[List[Dict]]:
        """
        Search for events related to a hostname.
        
        Args:
            hostname: Hostname to search for
            hours: Number of hours to look back (default: 24)
        
        Returns:
            List of events or None
        """
        query = f'host="{hostname}" OR hostname="{hostname}" OR dest="{hostname}" | head 1000'
        return self.search(query, earliest_time=f"-{hours}h")
    


