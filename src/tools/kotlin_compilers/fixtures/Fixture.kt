package fixture

inline fun twice(block: () -> Int): Int = block() + block()

class Box(val value: Int)

fun fixture(): Int = twice { Box(listOf(1, 2, 3).sum()).value }
