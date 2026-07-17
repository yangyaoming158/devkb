package com.example.broken;

/**
 * 语法错误 fixture：缺右花括号 + 游离 token，tree-sitter 应容错恢复可识别部分。
 */
public class HalfBroken {

    private int counter;

    public int increment() {
        counter++;
        return counter;
    }

    public void broken(  {
        this is not java at all %%%
}
