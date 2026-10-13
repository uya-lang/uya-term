UYA ?= $(HOME)/uya/uya/bin/uya
SRC := src/uyassh/main.uya
MODROOT := src/uyassh
BIN := bin/uya-ssh
PREFIX ?= $(HOME)/.local

.PHONY: all syntax build unit integration check fmt install clean

all: build

# 仅做词法/语法/类型检查
syntax:
	$(UYA) check --project-root $(MODROOT) $(SRC)

# 编译可执行文件
build:
	mkdir -p bin
	$(UYA) build $(SRC) -o $(BIN) --project-root $(MODROOT)

# 单元测试（协议编解码、扫描器、剪贴板仓库）
unit:
	@set -e; for f in tests/unit/test_*.uya; do \
		if [ -e "$$f" ]; then echo "== $$f"; $(UYA) test --project-root $(MODROOT) "$$f"; fi; \
	done

# 集成测试（伪终端端到端）
integration: build
	@set -e; for f in tests/integration/t*.sh; do \
		if [ -e "$$f" ]; then echo "== $$f"; sh "$$f"; fi; \
	done

# 验收命令：类型检查 + 单元测试 + 集成测试
check: syntax unit integration

fmt:
	@set -e; for f in $$(find src tests -name '*.uya' | sort); do $(UYA) fmt "$$f"; done

install: build
	mkdir -p $(PREFIX)/bin
	cp $(BIN) $(PREFIX)/bin/uya-ssh
	@echo "已安装到 $(PREFIX)/bin/uya-ssh"

clean:
	rm -rf bin build .uyacache tests/tmp
