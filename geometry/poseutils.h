#pragma once

// All arrays stored in contiguous matrices in row-major order
//
// I have two different representations of pose transformations:
//
// - Rt is a concatenated (4,3) array: Rt = nps.glue(R,t, axis=-2). The
//   transformation is R*x+t
//
// - rt is a concatenated (6) array: rt = nps.glue(r,t, axis=-1). The
//   transformation is R*x+t where R = R_from_r(r)

// Make an identity rotation or transformation
void mrcal_identity_R (double* R  /* (3,3) array */);
void mrcal_identity_r (double* r  /* (3)   array */);
void mrcal_identity_Rt(double* Rt /* (4,3) array */);
void mrcal_identity_rt(double* rt /* (6)   array */);

// Applying rotations. The R version is simply a matrix-vector multiplication
void mrcal_rotate_point_R( // output
                          double* x_out, // (3) array
                          double* J_R,   // (3,9) array. May be NULL
                          double* J_x,   // (3,3) array. May be NULL

                          // input
                          const double* R,
                          const double* x_in
                         );
void mrcal_rotate_point_r( // output
                          double* x_out, // (3) array
                          double* J_r,   // (3,3) array. May be NULL
                          double* J_x,   // (3,3) array. May be NULL

                          // input
                          const double* r,
                          const double* x_in
                         );

// Apply a transformation to a point
void mrcal_transform_point_Rt(// output
                              double* x_out, // (3) array
                              double* J_R,   // (3,9) array. Gradient.
                                             // Flattened R. May be NULL
                              double* J_t,   // (3,3) array. Gradient.
                                             // Flattened Rt. May be NULL
                              double* J_x,   // (3,3) array. Gradient. May be
                                             // NULL
                              // input
                              const double* Rt,  // (4,3) array
                              const double* x_in // (3) array
                              );
void mrcal_transform_point_rt(// output
                              double* x_out, // (3) array
                              double* J_r,   // (3,3) array. Gradient. May be
                                             // NULL
                              double* J_t,   // (3,3) array. Gradient. May be
                                             // NULL
                              double* J_x,   // (3,3) array. Gradient. May be
                                             // NULL
                              // input
                              const double* rt,  // (6) array
                              const double* x_in // (3) array
                              );

// Convert a rotation representation from a matrix to a Rodrigues vector
void mrcal_r_from_R( // output
                    double* r, // (3) vector
                    double* J, // (3,9) array. Gradient. May be NULL

                    // input
                    const double* R // (3,3) array
                   );

// Convert a rotation representation from a Rodrigues vector to a matrix
void mrcal_R_from_r( // outputs
                     double* R, // (3,3) array
                     double* J, // (9,3) array. Gradient. May be NULL

                     // input
                     const double* r // (3) vector
                    );

// Convert a transformation representation from Rt to rt. This is mostly a
// convenience functions since 99% of the work is done by mrcal_r_from_R(). No
// gradients available here. If you need gradients, call mrcal_r_from_R()
// directly
void mrcal_rt_from_Rt( // output
                      double* rt,  // (6) vector

                      // input
                      const double* Rt // (4,3) array
                     );

// Convert a transformation representation from Rt to rt. This is mostly a
// convenience functions since 99% of the work is done by mrcal_R_from_r(). No
// gradients available here. If you need gradients, call mrcal_R_from_r()
// directly
void mrcal_Rt_from_rt( // output
                      double* Rt, // (4,3) array

                      // input
                      const double* rt // (6) vector
                     );

// Invert an Rt transformation
//
// b = Ra + t  -> a = R'b - R't
void mrcal_invert_Rt( // output
                     double* Rt_out, // (4,3) array

                     // input
                     const double* Rt_in // (4,3) array
                    );

// Invert an rt transformation
//
// b = rotate(a) + t  -> a = invrotate(b) - invrotate(t)
void mrcal_invert_rt( // output
                     double* rt_out, // (6) array

                     // input
                     const double* rt_in // (6) array
                    );


// Compose two Rt transformations
//   R0*(R1*x + t1) + t0 =
//   R0*R1*x + R0*t1+t0
void mrcal_compose_Rt( // output
                      double* Rt_out, // (4,3) array

                      // input
                      const double* Rt_0, // (4,3) array
                      const double* Rt_1  // (4,3) array
                     );
// Compose two rt transformations
void mrcal_compose_rt( // output
                      double* rt_out, // (6) array

                      // input
                      const double* rt_0, // (6) array
                      const double* rt_1  // (6) array
                     );
