# Progress Report

**Intelligent Distributed Compute Fabric for Heterogeneous Personal Devices**

September 2026 · Second report

Rishu Raj – 23UCS085 · Abhay Singh – 23UCS097 · Aayush Kumar – 23UCS095 · Aditya Kumar Tiwari – 23UCS099

---

## Where We Are Now

The plan is written down version by version, the design is fixed and agreed, and the first part of the system
is built and working. Our laptops can now find each other over the network and stay connected: a laptop joins,
tells the main laptop what hardware it has, keeps reporting how busy it is, and reconnects by itself if the
network drops. Everything else in the project is built on top of this.

## What We Have Done Since the First Report

**Base setup.** Before writing the main system we set up the project base — the code structure, the database,
the logging, the test setup, and an automatic check that runs all our tests on both Windows and Linux every
time we change something.

**Small tests of the risky ideas.** Before building, we wrote seven small experiments to check whether the
ideas we depend on actually work. We tested how a laptop finds other laptops on the network, whether Windows
and Linux can talk to each other properly, how fast data moves between two machines, and whether a program
running in a container can use the graphics card. The container and graphics card test worked on our Linux
laptop. We also wrote a script that records exactly what hardware each laptop has.

**The core system.** What works today:

- Every laptop creates its own identity when it first starts, so the main laptop knows which machine is which.
- A laptop can join, and the system handles it correctly even if the same laptop connects twice.
- The main laptop knows within a few seconds if a machine has been switched off or disconnected.
- Each laptop detects its own CPU, memory, graphics card and disk, and keeps reporting how busy it is. This
  information is saved, because later our scheduling program will learn from it.
- If the main laptop is restarted, the others come back online in one to two seconds by themselves.

We are currently finishing two things: a command that shows all connected machines, and making the background
program start automatically when a laptop is switched on.

## One Change in Our Plan

In the first report we listed four ways of running work. After looking at the real amount of work involved, we
decided to **complete two of them properly — sending a whole task to one machine, and splitting a task across
several machines — instead of leaving all four half finished.** The other two, using another laptop's graphics
card and joining several graphics cards together, are fully designed and written down, and the system is built
so they can be added later without changing the design.

Choosing where to run the work, measuring everything, and the machine learning that predicts how long a task
will take remain in the plan as before.

## How We Are Going to Build It

For each difficult part of the system, this is what we do.

**Connecting the machines.** Every laptop runs a small program in the background. This program contacts the
main laptop by itself and keeps that connection open, sending a short "I am alive" message every two seconds.
We made it call out instead of waiting to be called, because on normal Wi-Fi a laptop usually cannot receive a
connection from outside. If the connection breaks, the program reconnects on its own and tells the main laptop
what it is still doing.

**Sending the code and the data.** Before a task can run somewhere else, its code and data must reach that
machine. Every file is given a short code calculated from the contents of the file itself. If two files have
the same code they are the same file. So before sending anything, we simply ask the other laptop which codes
it already has, and send only what is missing. The files travel directly from one laptop to another and not
through the main laptop, so it does not become a bottleneck. The same dataset is therefore downloaded only
once, no matter how many times it is used.

**Running the task safely on the other machine.** The task runs inside a container — a closed box on that
laptop with its own limited memory and processor share, and no access to the rest of the machine. Where a
container is not available, we use a separate Python environment instead. The environment is also given a code
of its own, so a machine sets it up once and reuses it after that.

**Dividing the work between unequal laptops.** This is the main problem, because our four laptops are not
equal — three have graphics cards and one has none. Instead of deciding in advance how much each laptop gets,
we cut the work into many small pieces and put them in a common list. Each laptop **takes** a piece when it is
free, finishes it, and takes the next one. A fast laptop naturally takes more pieces and a slow one takes
fewer, without anyone calculating it. The laptop without a graphics card still helps, just with fewer pieces.

**When a laptop is closed in the middle.** Every piece that a laptop takes is given a time limit. If that
laptop is shut down, loses Wi-Fi, or simply stops answering, the time limit runs out and the piece
automatically goes back into the common list, and another laptop picks it up. The job continues without being
restarted. If one piece is taking much longer than the rest at the end, we give the same piece to a free
machine as well and keep whichever result arrives first.

**Deciding where to run the work.** For every task, the system first lists all the possible ways of running it
— here, on one other laptop, or split across several. It removes the ones that are not possible, for example a
machine that does not have enough memory. Then it estimates how long each remaining option would take and
picks the best one. The estimate has two parts. The time to move the data we can calculate directly, because
we know how big the data is and we measure the network speed. The time to actually compute it we cannot
calculate, so the system remembers how long the same kind of work took on that machine before, and uses that.
In the beginning it uses simple rules, such as choosing the strongest free machine. As it collects real
measurements, a machine learning model improves the prediction, and if that model is not confident, the system
falls back to the simple calculation. Every decision is stored together with the options it rejected and what
actually happened, and that record is what the model learns from.

**Using another laptop's graphics card.** This is the most difficult part, and it is planned for later. Our
method is to work at the level of the AI library (PyTorch) rather than at the level of the graphics card
driver. The program keeps running on the user's laptop, but when it needs a calculation done on a graphics
card, the numbers are sent over the network to the machine that has one, the calculation happens there, and
only the result comes back. The model itself stays in that machine's graphics memory so it does not have to be
sent again and again. We are not trying to make the graphics card appear as if it were plugged into another
laptop, because that is not realistically possible over Wi-Fi.

**Keeping it secure.** This system runs code sent from one machine on another, so security is part of the
design from the start. Every laptop already creates its own private key the first time it runs. Joining the
group requires a one-time invitation code, and after that all communication between machines is encrypted and
each machine proves who it is. For now we test on our own trusted network, and the code clearly marks the
places where that assumption is made.

## How We Check That It Works

Every part goes through three separate steps: one agent writes the code, a second one writes tests from the
requirements and tries to break it, and a third reviews the design and the correctness and has to approve it.
A task is marked complete only after that, and only if the success conditions written in advance are met.

In the resource reporting part, the testing step found three serious bugs in the first version — including one
where repeated messages after a network drop corrupted the saved readings. All were fixed before that part was
accepted.

At the moment the project has about 2,600 automatic tests on the main system and about 1,600 on the
experiments, all passing, and 13 written design decisions recording why each major choice was made.

## What We Do Next

1. Run our hardware script on the other three laptops and record exactly what each one has.
2. Run the two-laptop network tests on real machines, on Wi-Fi and on cable, Windows to Linux and back. Everything
   so far has been tested on one machine and through automatic tests only.
3. Finish the current version — the command that lists the machines, and the background program starting
   automatically.
4. Then the next step: actually send a complete task to another laptop, run it there, and bring the result back.

## What We Need

- A network switch and cables, so we can also measure on a wired connection. Wi-Fi speed changes too much to make
  reliable measurements.
- Time with all four laptops together, because these tests cannot be done by one person on one machine.
