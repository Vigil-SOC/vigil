import logging
import sys

from services.medic.app.cli import main

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
sys.exit(main(sys.argv[1:]))
