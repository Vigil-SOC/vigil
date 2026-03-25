"""Local Services API - Manage local Docker containers (Splunk, PostgreSQL, etc.)."""

import subprocess
import logging
from typing import Dict, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


class ServiceAction(BaseModel):
    """Service action request."""
    action: str  # start, stop, restart, status


def run_docker_compose_command(service: str, action: str, profile: Optional[str] = None) -> Dict:
    """
    Run a docker-compose command for a service.
    
    Args:
        service: Service name (e.g., 'splunk', 'postgres')
        action: Action to perform ('up', 'down', 'restart', 'ps')
        profile: Optional docker-compose profile to use
    
    Returns:
        Dict with success status and output/error message
    """
    try:
        import os
        docker_dir = os.path.join(os.getcwd(), 'docker')
        
        if not os.path.exists(docker_dir):
            return {
                "success": False,
                "message": "Docker directory not found"
            }
        
        # Build command (support both v1 standalone and v2 plugin)
        import shutil
        if shutil.which('docker-compose'):
            cmd = ['docker-compose']
        else:
            cmd = ['docker', 'compose']
        
        if profile:
            cmd.extend(['--profile', profile])
        
        if action == 'up':
            cmd.extend(['up', '-d', service])
        elif action == 'down':
            cmd.extend(['stop', service])
        elif action == 'restart':
            cmd.extend(['restart', service])
        elif action == 'ps':
            cmd.extend(['ps', '--format', 'json'])
        else:
            return {"success": False, "message": f"Unknown action: {action}"}
        
        # Run command
        result = subprocess.run(
            cmd,
            cwd=docker_dir,
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0:
            return {
                "success": True,
                "message": f"Successfully {action}ed {service}",
                "output": result.stdout
            }
        else:
            return {
                "success": False,
                "message": f"Failed to {action} {service}",
                "error": result.stderr
            }
            
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": f"Command timed out after 60 seconds"
        }
    except Exception as e:
        logger.error(f"Error running docker-compose: {e}")
        return {
            "success": False,
            "message": f"Error: {str(e)}"
        }


def get_container_status(container_name: str) -> Dict:
    """
    Get the status of a Docker container.
    
    Args:
        container_name: Name of the container
    
    Returns:
        Dict with container status information
    """
    try:
        # Check if container exists and is running
        result = subprocess.run(
            ['docker', 'ps', '--filter', f'name={container_name}', '--format', '{{.Status}}'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0 and result.stdout.strip():
            status = result.stdout.strip()
            return {
                "running": True,
                "status": status,
                "container_name": container_name
            }
        
        # Check if container exists but is stopped
        result = subprocess.run(
            ['docker', 'ps', '-a', '--filter', f'name={container_name}', '--format', '{{.Status}}'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0 and result.stdout.strip():
            return {
                "running": False,
                "status": result.stdout.strip(),
                "container_name": container_name
            }
        
        return {
            "running": False,
            "status": "not found",
            "container_name": container_name
        }
        
    except Exception as e:
        logger.error(f"Error getting container status: {e}")
        return {
            "running": False,
            "status": "error",
            "error": str(e)
        }


@router.get("/splunk/status")
async def get_splunk_status():
    """
    Get Splunk Docker container status.
    
    Returns:
        Splunk container status and connection info
    """
    try:
        status = get_container_status("deeptempo-splunk")
        
        return {
            "installed": True,  # Docker compose file exists
            "running": status["running"],
            "status": status["status"],
            "container_name": "deeptempo-splunk",
            "web_url": "http://localhost:6990" if status["running"] else None,
            "hec_url": "http://localhost:8088" if status["running"] else None,
            "username": "admin",
            "note": "Default password: changeme123"
        }
    except Exception as e:
        logger.error(f"Error getting Splunk status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/splunk/start")
async def start_splunk():
    """
    Start the local Splunk Docker container.
    
    Returns:
        Success status and message
    """
    try:
        # Check if already running
        status = get_container_status("deeptempo-splunk")
        if status["running"]:
            return {
                "success": True,
                "message": "Splunk is already running",
                "web_url": "http://localhost:6990",
                "already_running": True
            }
        
        # Start Splunk with profile
        result = run_docker_compose_command("splunk", "up", profile="splunk")
        
        if result["success"]:
            return {
                "success": True,
                "message": "Splunk is starting. It may take 2-3 minutes to be fully ready.",
                "web_url": "http://localhost:6990",
                "hec_url": "http://localhost:8088",
                "username": "admin",
                "password": "changeme123",
                "note": "First startup may take several minutes. Check status endpoint for ready state."
            }
        else:
            raise HTTPException(status_code=500, detail=result.get("message", "Failed to start Splunk"))
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting Splunk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/splunk/stop")
async def stop_splunk():
    """
    Stop the local Splunk Docker container.
    
    Returns:
        Success status and message
    """
    try:
        result = run_docker_compose_command("splunk", "down", profile="splunk")
        
        if result["success"]:
            return {
                "success": True,
                "message": "Splunk stopped successfully"
            }
        else:
            raise HTTPException(status_code=500, detail=result.get("message", "Failed to stop Splunk"))
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error stopping Splunk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/splunk/restart")
async def restart_splunk():
    """
    Restart the local Splunk Docker container.
    
    Returns:
        Success status and message
    """
    try:
        result = run_docker_compose_command("splunk", "restart", profile="splunk")
        
        if result["success"]:
            return {
                "success": True,
                "message": "Splunk restarted successfully",
                "web_url": "http://localhost:6990"
            }
        else:
            raise HTTPException(status_code=500, detail=result.get("message", "Failed to restart Splunk"))
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error restarting Splunk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/postgres/status")
async def get_postgres_status():
    """
    Get PostgreSQL Docker container status.
    
    Returns:
        PostgreSQL container status and connection info
    """
    try:
        status = get_container_status("deeptempo-postgres")
        
        return {
            "installed": True,
            "running": status["running"],
            "status": status["status"],
            "container_name": "deeptempo-postgres",
            "host": "localhost",
            "port": 5432,
            "database": "deeptempo_soc"
        }
    except Exception as e:
        logger.error(f"Error getting PostgreSQL status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

