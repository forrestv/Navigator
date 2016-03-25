#!/usr/bin/python
'''
Model-Reference Adaptive Controller

################################################# BLACK BOX DESCRIPTION

Inputs:
    - desired pose waypoint [x, y, yaw] in world frame,
      i.e. an end goal of "GO HERE AND STAY"
    - current state (odometry) in world frame

Outputs:
    - the body frame wrench that should then be mapped to the thrusters

################################################# DETAILED DESCRIPTION

This controller implements typical PD feedback, but also adds on
a feedforward term to improve tracking.

The feedforward term makes use of an estimate of the boat's physical
parameters; specifically the effects of drag.

Instead of doing a bunch of experiments to find the true values of
these parameters ahead of time, this controller performs realtime
adaptation to determine them. The method it uses is tracking-error
gradient descent. To learn more, see <https://en.wikipedia.org/wiki/Adaptive_control>.

Additionally, this controller uses the "model-reference architecture."
<https://upload.wikimedia.org/wikipedia/commons/thumb/1/14/MRAC.svg/585px-MRAC.svg.png>
This essentially means that the trajectory generator is built into it.

The model reference used here is a boat with the same inertia and thrusters
as our actual boat, but drag that is such that the terminal velocities
achieved are those specified by self.vel_max_body. This vitual boat
moves with ease (no disturbances) directly to the desired waypoint goal. The
controller then tries to make the real boat track this "prescribed ideal" motion.

The way it was implemented here, you may only set a positional waypoint that
the controller will then plan out how to get to completely on its own. It always
intends to come to rest / station-hold at the waypoint you give it.

Until the distance to the x-y waypoint is less than self.heading_threshold,
the boat will try to point towards the x-y waypoint instead of pointing
in the direction specified by the desired yaw. "Smartyaw." It's, alright...
Set self.heading_threshold really high if you don't want it to smartyaw.

################################################# HOW TO USE

All tunable quantities are hard-coded in the __init__.
In addition to typical gains, you also have the ability to set a
"v_max_body", which is the maximum velocity in the body frame (i.e.
forwards, strafing, yawing) that the reference model will move with.

Use set_waypoint to give the controller A NEW desired waypoint. It
will overwrite the current desired pose with it, and reset the reference
model (the trajectory generator) to the boat's current state.

Use get_command to receive the necessary wrench for this instant.
That is, get_command publishes a ROS wrench message, and is fed
the current odometry and timestamp.

################################################# INTERNAL SEMANTICS

*_des: "desired", refers to the end goal, the "waypoint" we are trying to hold at.

*_ref: "reference", it is basically the generated trajectory, the "bread crumb",
       the instantaneous desired state on the way to the end goal waypoint.

*_body: "body frame", unless it is labelled with _body, it is in world-frame.

p: position
v: velocity
q: quaternion orientation
w: angular velocity
a: acceleration
aa: angular acceleration

################################################# OTHER

Author: Jason Nezvadovitz

'''
from __future__ import division
import rospy
import numpy as np
import numpy.linalg as npl
import tf.transformations as trns
from nav_msgs.msg import Odometry
from geometry_msgs.msg import Point, WrenchStamped, PoseStamped, Quaternion, Pose
from std_msgs.msg import Header

class MRAC_Controller:

    def __init__(self):
        '''
        Set hard-coded tunable parameters (gains and desired vehicle speed limit).
        The boat model section's values are used 

        '''
        #### TUNABLES
        # Proportional gains, body frame
        self.kp_body = np.diag([600, 700, 700])
        # Derivative gains, body frame
        self.kd_body = np.diag([650, 750, 750])
        # Disturbance adaptation rates, world frame
        self.ki = np.array([0.1, 0.1, 0.1])
        # Drag adaptation rates, world frame
        self.kg = np.array([3, 3, 3, 3, 3])
        # Initial disturbance estimate
        self.dist_est = np.array([0, 0, 0])
        # Initial drag estimate
        self.drag_est = np.array([0, 0, 0, 0, 0])  # [d1 d2 Lc1 Lc2 Lr]
        # User-imposed speed limit
        self.vel_max_body = np.array([1.5, 0.5, 0.5])  # [m/s, m/s, rad/s]
        # "Smart heading" threshold
        self.heading_threshold = 500  # m
        # Only use PD controller
        self.only_PD = True

        #### REFERENCE MODEL (note that this is not the adaptively estimated TRUE model; rather,
        #                     these parameters will govern the trajectory we want to achieve).
        self.mass_ref = 300  # kg
        self.inertia_ref = 300  # kg*m^2
        self.thrust_max = 220  # N
        self.thruster_positions = np.array([[-1.9000,  1.0000, -0.0123],
                                            [-1.9000, -1.0000, -0.0123],
                                            [ 1.6000, -0.6000, -0.0123],
                                            [ 1.6000,  0.6000, -0.0123]]) # back-left, back-right, front-right front-left, m
        self.thruster_directions = np.array([[ 0.7071,  0.7071,  0.0000],
                                             [ 0.7071, -0.7071,  0.0000],
                                             [ 0.7071,  0.7071,  0.0000],
                                             [ 0.7071, -0.7071,  0.0000]]) # back-left, back-right, front-right front-left
        self.lever_arms = np.cross(self.thruster_positions, self.thruster_directions)
        self.B_body = np.concatenate((self.thruster_directions.T, self.lever_arms.T))
        self.Fx_max_body = self.B_body.dot(self.thrust_max * np.array([1, 1, 1, 1]))
        self.Fy_max_body = self.B_body.dot(self.thrust_max * np.array([1, -1, 1, -1]))
        self.Mz_max_body = self.B_body.dot(self.thrust_max * np.array([-1, 1, 1, -1]))
        self.D_body = abs(np.array([self.Fx_max_body[0], self.Fy_max_body[1], self.Mz_max_body[5]])) / self.vel_max_body**2

        #### BASIC INITIALIZATIONS
        # Position waypoint
        self.p_des = np.array([0, 0])
        # Orientation waypoint
        self.q_des = np.array([1, 0, 0, 0])
        # Total distance that will be traversed for this waypoint
        self.traversal = 0
        # Reference states
        self.p_ref = np.array([0, 0])
        self.v_ref = np.array([0, 0])
        self.q_ref = np.array([0, 0, 0, 0])
        self.w_ref = 0
        self.a_ref = np.array([0, 0])
        self.aa_ref = 0

        #### ROS
        # Time since last controller call = 1/controller_call_frequency
        self.timestep = 0.02
        # For unpacking ROS messages later on
        self.position = np.zeros(2)
        self.orientation = np.array([1, 0, 0, 0])
        self.lin_vel = np.zeros(2)
        self.ang_vel = 0
        self.state = Odometry()
        # ayeeee subscrizzled
        rospy.Subscriber("/set_desired_pose", Point, self.set_waypoint)
        rospy.Subscriber("/odom", Odometry, self.get_command)
        self.last_odom = None
        self.wrench_pub = rospy.Publisher("/wrench/autonomous", WrenchStamped, queue_size=0)
        self.pose_ref_pub = rospy.Publisher("pose_ref", PoseStamped, queue_size=0)
        self.des_pose_pub = rospy.Publisher("desired_pose_ref", PoseStamped, queue_size=0)
        rospy.spin()


    def set_waypoint(self, msg):
        '''
        Sets desired waypoint ("GO HERE AND STAY").
        Resets reference model to current state (i.e. resets trajectory generation).

        '''
        # Set desired to user specified waypoint
        self.p_des = np.array([msg.x, msg.y])
        self.q_des = trns.quaternion_from_euler(0, 0, np.deg2rad(msg.z))
        self.traversal = npl.norm(self.p_des - self.position)
        self.p_ref = self.position
        self.v_ref = self.lin_vel
        self.q_ref = self.orientation
        self.w_ref = self.ang_vel
        self.a_ref = np.array([0, 0])
        self.aa_ref = 0

        self.des_pose_pub.publish(PoseStamped(
             header=Header(
                 frame_id='/enu',
             ),
             pose=Pose(
                 position=Point(msg.x, msg.y, 0),
                 orientation=Quaternion(*self.q_des),
             ),
        ))



    def get_command(self, msg):
        '''
        Publishes the wrench for this instant.
        (Note: this is called get_command because it used to be used for
        getting the actual thruster values, but now it is only being
        used for getting the wrench which is then later mapped elsewhere).

        '''

        # compute timestep from interval between this message's stamp and last's
        if self.last_odom is None:
            self.last_odom = msg
        else:
            self.timestep = (msg.header.stamp - self.last_odom.header.stamp).to_sec()
            self.last_odom = msg

        # ROS read-in
        position = msg.pose.pose.position
        orientation = msg.pose.pose.orientation
        lin_vel_body = msg.twist.twist.linear
        ang_vel_body = msg.twist.twist.angular

        # ROS unpack
        self.position = np.array([position.x, position.y])
        self.orientation = np.array([orientation.x, orientation.y, orientation.z, orientation.w])

        # Frame management quantities
        R = trns.quaternion_matrix(self.orientation)[:3, :3]
        y = trns.euler_from_quaternion(self.orientation)[2]

        # More ROS unpacking, converting body frame twist to world frame lin_vel and ang_vel
        self.lin_vel = R.dot(np.array([lin_vel_body.x, lin_vel_body.y, lin_vel_body.z]))[:2]
        self.ang_vel = R.dot(np.array([ang_vel_body.x, ang_vel_body.y, ang_vel_body.z]))[2]

        # Convert body PD gains to world frame
        kp = R.dot(self.kp_body).dot(R.T)
        kd = R.dot(self.kd_body).dot(R.T)

        # Compute error components (reference - true)
        p_err = self.p_ref - self.position
        y_err = trns.euler_from_quaternion(trns.quaternion_multiply(self.q_ref, trns.quaternion_inverse(self.orientation)))[2]
        v_err = self.v_ref - self.lin_vel
        w_err = self.w_ref - self.ang_vel

        # Combine error components into error vectors
        err = np.concatenate((p_err, [y_err]))
        errdot = np.concatenate((v_err, [w_err]))

        # Compute "anticipation" feedforward based on the boat's inertia
        inertial_feedforward = np.concatenate((self.a_ref, [self.aa_ref])) * [self.mass_ref, self.mass_ref, self.inertia_ref]

        # Compute the "learning" matrix
        drag_regressor = np.array([[               self.lin_vel[0]*np.cos(y)**2 + self.lin_vel[1]*np.sin(y)*np.cos(y),    self.lin_vel[0]/2 - (self.lin_vel[0]*np.cos(2*y))/2 - (self.lin_vel[1]*np.sin(2*y))/2,                             -self.ang_vel*np.sin(y),                               -self.ang_vel*np.cos(y),          0],
                                   [self.lin_vel[1]/2 - (self.lin_vel[1]*np.cos(2*y))/2 + (self.lin_vel[0]*np.sin(2*y))/2,                   self.lin_vel[1]*np.cos(y)**2 - self.lin_vel[0]*np.cos(y)*np.sin(y),                              self.ang_vel*np.cos(y),                               -self.ang_vel*np.sin(y),          0],
                                   [                                                                     0,                                                                         0,    self.lin_vel[1]*np.cos(y) - self.lin_vel[0]*np.sin(y),    - self.lin_vel[0]*np.cos(y) - self.lin_vel[1]*np.sin(y),    self.ang_vel]])

        # wrench = PD + feedforward + I + adaptation
        if self.only_PD:
            wrench = (kp.dot(err)) + (kd.dot(errdot))
        else:
            wrench = (kp.dot(err)) + (kd.dot(errdot)) + inertial_feedforward + self.dist_est + (drag_regressor.dot(self.drag_est))

        # Update disturbance estimate, drag estimates, and model reference for the next call
        self.dist_est = self.dist_est + (self.ki * err * self.timestep)
        self.drag_est = self.drag_est + (self.kg * (drag_regressor.T.dot(err + errdot)) * self.timestep)
        self.increment_reference()

        # ROS CURRENTLY TAKES WRENCHES IN BODY FRAME FORWARD-RIGHT-DOWN, NOT SURE WHY <<< to be fixed at some point
        wrench_body = R.T.dot(wrench)
        wrench_body[1:] = -wrench_body[1:]

        # SAFETY SATURATION, NOT SURE WHY THIS NEEDS TO BE DONE IN SOFTWARE BUT RIGHT NOW IT DOES <<< to be fixed at some point
        wrench_body[0] = np.clip(wrench_body[0], -600, 600)
        wrench_body[1] = np.clip(wrench_body[1], -600, 600)
        wrench_body[2] = np.clip(wrench_body[2], -600, 600)

        # NOT NEEDED SINCE WE ARE USING A DIFFERENT NODE FOR ACTUAL THRUSTER MAPPING
        # # Compute world frame thruster matrix (B) from thruster geometry, and then map wrench to thrusts
        # B = np.concatenate((R.dot(self.thruster_directions.T), R.dot(self.lever_arms.T)))
        # B_3dof = np.concatenate((B[:2, :], [B[5, :]]))
        # command = self.thruster_mapper(wrench, B_3dof)

        # Give wrench to ROS
        to_send = WrenchStamped()
        to_send.header.frame_id = "/base_link"
        to_send.wrench.force.x = wrench_body[0]
        to_send.wrench.force.y = wrench_body[1]
        to_send.wrench.torque.z = wrench_body[2]
        self.wrench_pub.publish(to_send)

        self.pose_ref_pub.publish(PoseStamped(
             header=Header(
                 frame_id='/enu',
                 stamp=msg.header.stamp,
             ),
             pose=Pose(
                 position=Point(self.p_ref[0], self.p_ref[1], 0),
                 orientation=Quaternion(*self.q_ref),
             ),
        ))


    def increment_reference(self):
        '''
        Steps the model reference (trajectory to track) by one self.timestep.

        '''
        # Frame management quantities
        R_ref = trns.quaternion_matrix(self.q_ref)[:3, :3]
        y_ref = trns.euler_from_quaternion(self.q_ref)[2]

        # Convert body PD gains to world frame
        kp = R_ref.dot(self.kp_body).dot(R_ref.T)
        kd = R_ref.dot(self.kd_body).dot(R_ref.T)

        # Compute error components (desired - reference), using "smartyaw"
        p_err = self.p_des - self.p_ref
        v_err = -self.v_ref
        w_err = -self.w_ref
        if npl.norm(p_err) <= self.heading_threshold:
            q_err = trns.quaternion_multiply(self.q_des, trns.quaternion_inverse(self.q_ref))
        else:
            q_direct = trns.quaternion_from_euler(0, 0, np.angle(p_err[0] + (1j * p_err[1])))
            q_err = trns.quaternion_multiply(q_direct, trns.quaternion_inverse(self.q_ref))
        y_err = trns.euler_from_quaternion(q_err)[2]

        # Combine error components into error vectors
        err = np.concatenate((p_err, [y_err]))
        errdot = np.concatenate((v_err, [w_err]))
        wrench = (kp.dot(err)) + (kd.dot(errdot))

        # Compute world frame thruster matrix (B) from thruster geometry, and then map wrench to thrusts
        B = np.concatenate((R_ref.dot(self.thruster_directions.T), R_ref.dot(self.lever_arms.T)))
        B_3dof = np.concatenate((B[:2, :], [B[5, :]]))
        command = self.thruster_mapper(wrench, B_3dof)
        wrench_saturated = B.dot(command)

        # Use model drag to find drag force on virtual boat
        twist_body = R_ref.T.dot(np.concatenate((self.v_ref, [self.w_ref])))
        drag_ref = R_ref.dot(self.D_body * twist_body * abs(twist_body))

        # Step forward the dynamics of the virtual boat
        self.a_ref = (wrench_saturated[:2] - drag_ref[:2]) / self.mass_ref
        self.aa_ref = (wrench_saturated[5] - drag_ref[2]) / self.inertia_ref
        self.p_ref = self.p_ref + (self.v_ref * self.timestep)
        self.q_ref = trns.quaternion_from_euler(0, 0, y_ref + (self.w_ref * self.timestep))
        self.v_ref = self.v_ref + (self.a_ref * self.timestep)
        self.w_ref = self.w_ref + (self.aa_ref * self.timestep)


    def thruster_mapper(self, wrench, B):
        '''
        Virtual thruster mapper used by the model reference.
        Math-wise, it is the same as the thruster mapper used by
        the actual boat.

        '''
        # Get minimum energy mapping using pseudoinverse
        command = npl.pinv(B).dot(wrench)

        # Scale back commands to maximums
        command_max = np.max(np.abs(command))
        if command_max > self.thrust_max:
            command = (self.thrust_max / command_max) * command

        return command


# ROS ME BABY
if __name__ == '__main__':

    rospy.init_node("controller")

    controller = MRAC_Controller()