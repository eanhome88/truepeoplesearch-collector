//go:build windows

package main

import (
	"fmt"
	"os/exec"
	"syscall"
	"unsafe"
)

var (
	kernel32                 = syscall.NewLazyDLL("kernel32.dll")
	createJobObject          = kernel32.NewProc("CreateJobObjectW")
	setInformationJobObject  = kernel32.NewProc("SetInformationJobObject")
	assignProcessToJobObject = kernel32.NewProc("AssignProcessToJobObject")
	terminateJobObject       = kernel32.NewProc("TerminateJobObject")
	createToolhelp32Snapshot = kernel32.NewProc("CreateToolhelp32Snapshot")
	thread32First            = kernel32.NewProc("Thread32First")
	thread32Next             = kernel32.NewProc("Thread32Next")
	openThread               = kernel32.NewProc("OpenThread")
	resumeThread             = kernel32.NewProc("ResumeThread")
)

type basicJobLimits struct {
	ProcessTime, JobTime         int64
	Flags                        uint32
	MinWorkingSet, MaxWorkingSet uintptr
	ActiveProcesses              uint32
	Affinity                     uintptr
	Priority, Scheduling         uint32
}

type extendedJobLimits struct {
	Basic                            basicJobLimits
	IO                               [6]uint64
	ProcessMemory, JobMemory         uintptr
	PeakProcessMemory, PeakJobMemory uintptr
}

type threadEntry struct {
	Size, Usage, ID, Owner uint32
	BasePriority, Delta    int32
	Flags                  uint32
}

type windowsProcessGuard struct{ job syscall.Handle }

func newProcessGuard(cmd *exec.Cmd) (processGuard, error) {
	job, _, err := createJobObject.Call(0, 0)
	if job == 0 {
		return nil, fmt.Errorf("create owned process job: %w", err)
	}
	guard := &windowsProcessGuard{job: syscall.Handle(job)}
	limits := extendedJobLimits{}
	limits.Basic.Flags = 0x2000 // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	ok, _, err := setInformationJobObject.Call(job, 9, uintptr(unsafe.Pointer(&limits)), unsafe.Sizeof(limits))
	if ok == 0 {
		_ = guard.close()
		return nil, fmt.Errorf("configure owned process job: %w", err)
	}
	// No Python/user code executes before job ownership is established. This
	// prevents children escaping in the Start/AssignProcessToJobObject race.
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x4 | 0x200} // SUSPENDED | NEW_PROCESS_GROUP
	return guard, nil
}

func (guard *windowsProcessGuard) attach(cmd *exec.Cmd) error {
	handle, err := syscall.OpenProcess(0x100|0x1, false, uint32(cmd.Process.Pid)) // SET_QUOTA | TERMINATE
	if err != nil {
		return fmt.Errorf("open owned supervisor: %w", err)
	}
	defer syscall.CloseHandle(handle)
	ok, _, callErr := assignProcessToJobObject.Call(uintptr(guard.job), uintptr(handle))
	if ok == 0 {
		return fmt.Errorf("assign owned supervisor job: %w", callErr)
	}
	// os/exec closes the initial thread handle; find only the suspended thread
	// of our own freshly-created PID, then resume it after job assignment.
	snapshot, _, callErr := createToolhelp32Snapshot.Call(0x4, 0) // SNAPTHREAD
	if snapshot == ^uintptr(0) {
		return fmt.Errorf("find owned supervisor thread: %w", callErr)
	}
	defer syscall.CloseHandle(syscall.Handle(snapshot))
	entry := threadEntry{Size: uint32(unsafe.Sizeof(threadEntry{}))}
	ok, _, callErr = thread32First.Call(snapshot, uintptr(unsafe.Pointer(&entry)))
	for ok != 0 {
		if entry.Owner == uint32(cmd.Process.Pid) {
			thread, _, err := openThread.Call(0x2, 0, uintptr(entry.ID)) // SUSPEND_RESUME
			if thread == 0 {
				return fmt.Errorf("open owned supervisor thread: %w", err)
			}
			resumed, _, err := resumeThread.Call(thread)
			_ = syscall.CloseHandle(syscall.Handle(thread))
			if uint32(resumed) == ^uint32(0) {
				return fmt.Errorf("resume owned supervisor: %w", err)
			}
			return nil
		}
		entry.Size = uint32(unsafe.Sizeof(entry))
		ok, _, callErr = thread32Next.Call(snapshot, uintptr(unsafe.Pointer(&entry)))
	}
	return fmt.Errorf("owned supervisor thread unavailable: %v", callErr)
}

func (guard *windowsProcessGuard) stop(cmd *exec.Cmd) error {
	// Windows does not provide POSIX SIGTERM semantics for console Python.
	// Terminate the owned job, not a PID-file target or a process-name wildcard.
	return guard.force(cmd)
}

func (guard *windowsProcessGuard) force(cmd *exec.Cmd) error {
	ok, _, err := terminateJobObject.Call(uintptr(guard.job), 1)
	if ok == 0 {
		return fmt.Errorf("terminate owned process job: %w", err)
	}
	return nil
}

func (guard *windowsProcessGuard) close() error {
	if guard.job == 0 {
		return nil
	}
	err := syscall.CloseHandle(guard.job)
	guard.job = 0
	return err
}
