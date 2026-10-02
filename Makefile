.PHONY: setup bench report test patches
# Some hosts shadow `cc` with a non-compiler.
CCENV := $(shell cc --version >/dev/null 2>&1 || echo CC=gcc CXX=g++ CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=gcc)
setup:
	scripts/setup.sh
bench:
	scripts/bench.sh
# make report RUN=runs/<id>
report:
	RUN=$(RUN) STAGES=report scripts/bench.sh
test:
	cd vendor/local-pir-rpc && $(CCENV) cargo test --release -q
	cd vendor/kohaku-rs/crates/pir-provider && $(CCENV) cargo test -q
patches:
	for d in local-pir-rpc kohaku-rs inspire-gpu-serving; do \
	  (cd vendor/$$d && git add -N . && git diff -- . ':!Cargo.lock' > ../../patches/$$d.patch); done
