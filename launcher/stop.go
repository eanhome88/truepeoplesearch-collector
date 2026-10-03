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
	fmt.Println("  TruePeopleSearch - 正在停止本次客户控制台启动的服务...")
	fmt.Println("============================================================")

	pythonExe := findPython(rootDir)
	if pythonExe == "" {
		fmt.Println("❌ 未找到 Python 3.9+ 运行环境；未执行停止操作。")
		os.Exit(1)
	}

	supScript := filepath.Join(rootDir, "scripts", "tps_supervisor.py")
	stopCmd := exec.Command(pythonExe, supScript, "stop", "--dashboard-only")
	stopCmd.Dir = rootDir
	if err := stopCmd.Run(); err != nil {
		fmt.Println("⚠️ 未停止任何服务：目标不是本次客户控制台启动的实例。")
		os.Exit(1)
	}

	fmt.Println("✅ 客户控制台停止请求已完成。窗口将在 2 秒后关闭。")
	time.Sleep(2 * time.Second)
}
