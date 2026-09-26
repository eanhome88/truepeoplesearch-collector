package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

func findPython(rootDir string) string {
	candidates := []string{
		filepath.Join(rootDir, ".venv", "Scripts", "python.exe"),
		filepath.Join(rootDir, ".venv", "bin", "python3"),
		filepath.Join(rootDir, "venv", "Scripts", "python.exe"),
	}
	for _, c := range candidates {
		if _, err := os.Stat(c); err == nil {
			return c
		}
	}
	if p, err := exec.LookPath("python.exe"); err == nil {
		return p
	}
	if p, err := exec.LookPath("python3"); err == nil {
		return p
	}
	if p, err := exec.LookPath("python"); err == nil {
		return p
	}
	return ""
}

func main() {
	execPath, err := os.Executable()
	if err != nil {
		execPath, _ = filepath.Abs(".")
	}
	rootDir := filepath.Dir(execPath)
	_ = os.Chdir(rootDir)

	fmt.Println("============================================================")
	fmt.Println("  TruePeopleSearch - 正在平稳停止所有后台服务...")
	fmt.Println("============================================================")

	pythonExe := findPython(rootDir)
	if pythonExe == "" {
		pythonExe = "python"
	}

	supScript := filepath.Join(rootDir, "scripts", "tps_supervisor.py")
	stopCmd := exec.Command(pythonExe, supScript, "stop")
	stopCmd.Dir = rootDir
	_ = stopCmd.Run()

	fmt.Println("✅ 后台服务已停止完毕。窗口将在 2 秒后关闭。")
	time.Sleep(2 * time.Second)
}
