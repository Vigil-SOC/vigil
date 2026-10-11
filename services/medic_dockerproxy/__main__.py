import sys

from services.medic_dockerproxy.cli import main

sys.exit(main(sys.argv[1:]))
