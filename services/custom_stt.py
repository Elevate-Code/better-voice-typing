"""Custom Speech-to-Text Service Implementation"""
import os
import logging
from typing import Union, Optional
from pathlib import Path
import io
import requests
import json

logger = logging.getLogger('voice_typing')


class CustomTranscriber:
    """Custom STT service implementation for local or remote endpoints"""

    def __init__(
        self,
        base_url: str = "http://192.168.0.5:8000",
        model: str = "parakeet-tdt-0.6b-v2",
        language: str = "en"
    ):
        """
        Initialize custom transcriber

        Args:
            base_url: Base URL of the custom STT endpoint (local or remote)
            model: Model to use for transcription
            language: Language code for transcription
        """
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.language = language
        
        # Get API key if configured (optional for local models)
        self.api_key = os.environ.get("CUSTOM_STT_API_KEY")
        
        logger.info(f"Initialized custom transcriber with URL: {self.base_url}, model: {model}")

    def transcribe(self, audio_data: Union[bytes, str, Path]) -> str:
        """
        Transcribe audio using custom endpoint

        Args:
            audio_data: Either raw audio bytes, file path as string, or Path object

        Returns:
            Transcribed text

        Raises:
            Exception: If transcription fails
        """
        try:
            # Prepare audio data
            if isinstance(audio_data, (str, Path)):
                file_path = Path(audio_data)
                if not file_path.exists():
                    raise FileNotFoundError(f"Audio file not found: {file_path}")
                with open(file_path, 'rb') as f:
                    audio_bytes = f.read()
                filename = file_path.name
            else:
                audio_bytes = audio_data
                filename = "audio.wav"

            # Add authorization header if API key is configured
            headers = {}
            if self.api_key:
                headers['Authorization'] = f"Bearer {self.api_key}"
            
            # Try common endpoint patterns
            endpoints = [
                f"{self.base_url}/transcribe",                # Simple format
                f"{self.base_url}/v1/audio/transcriptions",  # OpenAI API v1 format
                f"{self.base_url}/api/transcribe",            # API prefix format
            ]
            
            last_error = None
            for endpoint in endpoints:
                # Prepare the request with 'file' field (common standard)
                files = {
                    'file': (filename, io.BytesIO(audio_bytes), 'audio/wav')
                }
                
                logger.debug(f"Trying endpoint: {endpoint}")
                
                # First try: just the file (minimal request)
                try:
                    response = requests.post(
                        endpoint,
                        files=files,
                        headers=headers,
                        timeout=60
                    )
                    
                    if response.status_code == 200:
                        return self._parse_response(response.json())
                        
                    elif response.status_code == 422 or response.status_code == 400:
                        # Try with model parameter
                        logger.debug(f"Got {response.status_code}, trying with model parameter")
                        files = {
                            'file': (filename, io.BytesIO(audio_bytes), 'audio/wav')
                        }
                        data = {
                            'model': self.model
                        }
                        
                        response = requests.post(
                            endpoint,
                            files=files,
                            data=data,
                            headers=headers,
                            timeout=60
                        )
                        
                        if response.status_code == 200:
                            return self._parse_response(response.json())
                        else:
                            last_error = f"HTTP {response.status_code}: {response.text}"
                            
                    elif response.status_code == 404:
                        # Endpoint doesn't exist, try next one
                        last_error = f"Endpoint not found: {endpoint}"
                        continue
                    else:
                        last_error = f"HTTP {response.status_code}: {response.text}"
                        
                except requests.exceptions.ConnectionError:
                    last_error = f"Connection failed to {endpoint}"
                    continue
                except requests.exceptions.Timeout:
                    last_error = f"Request timeout to {endpoint}"
                    continue
                except Exception as e:
                    last_error = str(e)
                    continue
            
            # If we get here, all endpoints failed
            error_msg = f"Custom transcription failed. Last error: {last_error}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
                
        except Exception as e:
            logger.error(f"Custom transcription failed: {e}", exc_info=True)
            raise

    def _parse_response(self, result) -> str:
        """
        Parse the response from the custom STT API
        
        Args:
            result: The JSON response from the API
            
        Returns:
            The extracted transcription text
        """
        if isinstance(result, dict):
            # Check for segments format (some models return this)
            if 'segments' in result and isinstance(result['segments'], list):
                # Extract text from all segments and join them
                texts = []
                for segment in result['segments']:
                    if isinstance(segment, dict) and 'text' in segment:
                        texts.append(segment['text'])
                if texts:
                    return ' '.join(texts)
            
            # Try common field names
            for field in ['text', 'transcription', 'result', 'transcript', 'output', 'response', 'data']:
                if field in result:
                    return str(result[field])
            
            # If no known field, log warning and return the whole dict as string
            logger.warning(f"Unknown response format: {result}")
            return str(result)
        elif isinstance(result, str):
            return result
        else:
            return str(result)

    def update_model(self, model: str) -> None:
        """Update the model used for transcription"""
        self.model = model
        logger.info(f"Updated custom STT model to: {model}")

    def update_language(self, language: str) -> None:
        """Update the language used for transcription"""
        self.language = language
        logger.info(f"Updated custom STT language to: {language}")
    
    def update_base_url(self, base_url: str) -> None:
        """Update the base URL for the custom endpoint"""
        self.base_url = base_url.rstrip('/')
        logger.info(f"Updated custom STT base URL to: {self.base_url}")