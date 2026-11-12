import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if "--setup" in sys.argv:
    from shared_cockpit.install_tasks import setup
    setup()
elif "--remove-shortcuts" in sys.argv:
    from shared_cockpit.install_tasks import remove_shortcuts
    remove_shortcuts()
else:
    from shared_cockpit.app import main
    main()
