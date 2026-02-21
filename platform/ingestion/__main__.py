"""python -m ingestion"""
import asyncio
from .ingestor import main

asyncio.run(main())
