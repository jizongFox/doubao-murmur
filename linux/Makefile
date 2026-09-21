.PHONY: install run test clean deb flatpak-build flatpak-install

# Install Python dependencies
install:
	pip3 install --user -e ".[dev]"

# Run the app
run:
	PYTHONPATH=src python3 -m doubao_murmur

# Run tests
test:
	PYTHONPATH=src python3 -m pytest tests/ -v

# Build a native Debian/Ubuntu package
deb:
	sh ./debian/build.sh

# Build Flatpak (uses the org.flatpak.Builder flatpak; install with:
#   flatpak install flathub org.flatpak.Builder)
FLATPAK_BUILDER = flatpak run org.flatpak.Builder

flatpak-build:
	$(FLATPAK_BUILDER) --force-clean --user build-dir flatpak/com.doubao.Murmur.yml

# Build and install locally for the current user
flatpak-install:
	$(FLATPAK_BUILDER) --force-clean --user --install build-dir flatpak/com.doubao.Murmur.yml

# Clean build artifacts
clean:
	rm -rf build-dir repo .flatpak-builder
	rm -rf src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
