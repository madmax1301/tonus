# tonus-navidrome-plugin — Makefile
#
# Targets:
#   make build         build plugin.wasm (TinyGo if available, plain Go fallback)
#   make package       build + zip into tonus-navidrome-plugin.ndp
#   make install-local copy .ndp into NAVIDROME_PLUGINS_DIR
#   make clean         remove build artifacts
#
# TinyGo gives us a ~100 KB wasm; plain Go produces several MB but works
# without TinyGo installed. Both are wasip1-compatible.

PLUGIN_NAME       := tonus-navidrome-plugin
WASM_FILE         := plugin.wasm
NDP_FILE          := $(PLUGIN_NAME).ndp
TINYGO            := $(shell command -v tinygo 2>/dev/null)
NAVIDROME_PLUGINS_DIR ?= $(HOME)/.navidrome/plugins

.PHONY: build package install-local clean help

help:
	@echo "tonus-navidrome-plugin"
	@echo "  build              Build $(WASM_FILE) (TinyGo preferred)"
	@echo "  package            Build + zip into $(NDP_FILE)"
	@echo "  install-local      Copy $(NDP_FILE) into $(NAVIDROME_PLUGINS_DIR)"
	@echo "  clean              Remove build artifacts"
	@echo
	@echo "Override NAVIDROME_PLUGINS_DIR if your install path differs:"
	@echo "  NAVIDROME_PLUGINS_DIR=/srv/navidrome/plugins make install-local"

build:
ifdef TINYGO
	@echo "Building with TinyGo..."
	tinygo build -opt=2 -scheduler=none -no-debug -gc=leaking \
		-o $(WASM_FILE) -target wasip1 -buildmode=c-shared .
else
	@echo "TinyGo not found — falling back to plain Go (.wasm will be much larger)"
	GOOS=wasip1 GOARCH=wasm go build -buildmode=c-shared -o $(WASM_FILE) .
endif
	@echo "→ $(WASM_FILE) built ($(shell du -h $(WASM_FILE) 2>/dev/null | cut -f1))"

package: build
	@rm -f $(NDP_FILE)
	zip -j $(NDP_FILE) $(WASM_FILE) manifest.json
	@echo "→ $(NDP_FILE) ready (upload via Navidrome → Settings → Plugins)"

install-local: package
	@mkdir -p $(NAVIDROME_PLUGINS_DIR)
	cp $(NDP_FILE) $(NAVIDROME_PLUGINS_DIR)/
	@echo "→ Installed into $(NAVIDROME_PLUGINS_DIR)/$(NDP_FILE)"
	@echo "  Restart Navidrome (or trigger plugin reload) to pick it up."

clean:
	rm -f $(WASM_FILE) $(NDP_FILE)
	@echo "→ cleaned"
