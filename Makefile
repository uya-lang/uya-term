UYA ?= $(HOME)/uya/uya/bin/uya
SRC := src/uyassh/main.uya
MODROOT := src/uyassh
BIN := bin/uya-ssh
PREFIX ?= $(HOME)/.local

# 静态链接开关：默认开（零依赖静态 ELF，不依赖 ld.so / libc.so）。
# STATIC=0 回退动态链接（宿主机缺少静态 libc.a 时使用）；集成测试 t11 只验证静态产物。
STATIC ?= 1
ifeq ($(STATIC),0)
LDFLAGS := $(filter-out -static,$(LDFLAGS))
else
LDFLAGS += -static
endif
export LDFLAGS
export STATIC

.PHONY: all syntax build unit integration check fmt install clean

all: build

# 仅做词法/语法/类型检查
syntax:
	$(UYA) check --project-root $(MODROOT) $(SRC)

# 编译可执行文件（默认静态：LDFLAGS 透传给 uya build 内部的链接步骤）
build:
	mkdir -p bin
	@$(UYA) build $(SRC) -o $(BIN) --project-root $(MODROOT) || { \
		echo "提示: 静态链接需要宿主机提供静态 libc（glibc-static / libc.a）；" >&2; \
		echo "      若确实没有，可改用 make build STATIC=0 回退动态链接。" >&2; \
		exit 1; \
	}

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
	@set -e; for f in tests/integration/t*.py; do \
		if [ -e "$$f" ]; then echo "== $$f"; python3 "$$f"; fi; \
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
