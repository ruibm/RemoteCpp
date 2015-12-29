SHELL = bash

all: binary

run: binary
	@echo -e '\nRunning the binary now...'
	./SampleCppProject.bin

binary:
	@echo -e '\nBuilding the binary...'
	g++ -o SampleCppProject.bin *.cpp *.cc *.c

clean:
	@echo -e '\nCleaning up the build dir...'
	rm -f *.o *.bin

