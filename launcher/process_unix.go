//go:build darwin || linux

package main

import (
	"errors"
	"os/exec"
	"syscall"
)

type unixProcessGuard struct{}

func newProcessGuard(cmd *exec.Cmd) (processGuard, error) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	return &unixProcessGuard{}, nil
}

func (*unixProcessGuard) attach(cmd *exec.Cmd) error { return nil }

func signalOwnedGroup(cmd *exec.Cmd, signal syscall.Signal) error {
	err := syscall.Kill(-cmd.Process.Pid, signal)
	if errors.Is(err, syscall.ESRCH) {
		return nil
	}
	return err
}

func (*unixProcessGuard) stop(cmd *exec.Cmd) error {
	return signalOwnedGroup(cmd, syscall.SIGTERM)
}

func (*unixProcessGuard) force(cmd *exec.Cmd) error {
	return signalOwnedGroup(cmd, syscall.SIGKILL)
}

func (*unixProcessGuard) close() error { return nil }
